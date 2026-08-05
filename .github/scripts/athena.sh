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
    for table in electricity_consumption meter_interval_reading ghg_scope_1_activity \
                 ghg_scope_3_activity procurement_fuel_spend general_ledger_posting \
                 financial_statement_extract model_evaluation_prediction \
                 model_evaluation_confusion risk_register incident_log; do
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
