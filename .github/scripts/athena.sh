#!/usr/bin/env bash
# Athena statements that are not resolver queries: partition repair and the analytics views.
#
# Kept out of `queries/` on purpose. Everything there produces a published figure, is scoped
# by a bound tenant parameter and has its text hashed into a lineage record. None of that is
# true here, and mixing them would make the lineage hash meaningless.
set -euo pipefail

action="${1:?repair|views}"
workgroup="${ATTESTOR_WORKGROUP:-attestor}"
database="${ATTESTOR_RAW_DATABASE:-attestor_raw}"

run() {
  local sql="$1" db="$2"
  local id
  id=$(aws athena start-query-execution \
    --query-string "$sql" \
    --work-group "$workgroup" \
    --query-execution-context "Database=$db" \
    --query QueryExecutionId --output text)
  while true; do
    state=$(aws athena get-query-execution --query-execution-id "$id" \
      --query 'QueryExecution.Status.State' --output text)
    case "$state" in
      SUCCEEDED) return 0 ;;
      FAILED|CANCELLED)
        aws athena get-query-execution --query-execution-id "$id" \
          --query 'QueryExecution.Status.StateChangeReason' --output text >&2
        return 1 ;;
    esac
    sleep 2
  done
}

case "$action" in
  repair)
    # The seed writes `tenant_id=` prefixes; without this the partitions exist in S3 and not
    # in the catalogue, and every query returns zero rows while looking perfectly healthy.
    #
    # The list used to be typed here, and the sentence above turned out to be a description of
    # what happens when it falls behind rather than a warning against it. `security_scan_result`
    # was added to Terraform, to the seed and to dbt, and not to this line — so its data reached
    # S3, its partition was never registered, `stg_security_scan_result` selected from nothing,
    # dbt built the gold table with `OK 0`, and every data-contract test passed because
    # `accepted_values` over zero rows is vacuously true. Deploy 31454723596 got to the last
    # verification step before anything said so.
    #
    # Asking the catalogue removes the copy. A table Terraform creates is repaired because it
    # exists, not because somebody remembered it twice.
    tables=$(aws glue get-tables --database-name "$database" \
      --query 'TableList[?PartitionKeys[?Name==`tenant_id`]].Name' --output text)
    if [ -z "$tables" ]; then
      echo "no partitioned tables in ${database}; the data layer has not been applied" >&2
      exit 1
    fi
    for table in $tables; do
      echo "── repairing $table"
      run "MSCK REPAIR TABLE ${database}.${table}" "$database"
    done
    ;;
  views)
    # Statements are separated by `;` in one file; Athena takes one at a time.
    python - <<'PY' > /tmp/views.txt
import pathlib, re
sql = pathlib.Path("analytics/views.sql").read_text()
sql = re.sub(r"--[^\n]*", "", sql)
statements = [s.strip() for s in sql.split(";") if s.strip()]
print("\x00".join(statements), end="")
PY
    while IFS= read -r -d '' statement; do
      echo "── ${statement:0:70}..."
      run "$statement" "${ATTESTOR_DATABASE:-attestor_gold}"
    done < /tmp/views.txt
    ;;
  *) echo "unknown action $action" >&2; exit 2 ;;
esac
