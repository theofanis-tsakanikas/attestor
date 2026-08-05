#!/usr/bin/env bash
# Start an ingestion job per data source and wait for it.
#
# Waiting matters: a knowledge base whose sync is still running answers retrieval queries
# with whatever it had before, so a report generated straight after a deploy would cite the
# previous period's evidence and look entirely normal.
set -euo pipefail

kb_evidence=$(terraform -chdir=infra/knowledge output -raw evidence_kb_id)
kb_regulatory=$(terraform -chdir=infra/knowledge output -raw regulatory_kb_id)

sync_one() {
  local kb="$1" ds="$2"
  local job
  job=$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "$kb" --data-source-id "$ds" \
    --query 'ingestionJob.ingestionJobId' --output text)
  echo "   job $job"
  while true; do
    status=$(aws bedrock-agent get-ingestion-job \
      --knowledge-base-id "$kb" --data-source-id "$ds" --ingestion-job-id "$job" \
      --query 'ingestionJob.status' --output text)
    case "$status" in
      COMPLETE) return 0 ;;
      FAILED)
        aws bedrock-agent get-ingestion-job --knowledge-base-id "$kb" --data-source-id "$ds" \
          --ingestion-job-id "$job" --query 'ingestionJob.failureReasons' --output text >&2
        return 1 ;;
    esac
    sleep 10
  done
}

for kb in "$kb_evidence" "$kb_regulatory"; do
  sources=$(aws bedrock-agent list-data-sources --knowledge-base-id "$kb" \
    --query 'dataSourceSummaries[].dataSourceId' --output text)

  # A knowledge base with no data source used to pass through this loop in silence. That is
  # how the regulatory corpus came to be permanently empty while every step of the deploy
  # reported success — there was nothing to sync, so nothing failed. An empty list is now the
  # loudest thing this script can say.
  if [ -z "$sources" ]; then
    echo "knowledge base $kb has no data source; it would answer every query with nothing" >&2
    exit 1
  fi

  for ds in $sources; do
    echo "── syncing $kb / $ds"
    sync_one "$kb" "$ds"
  done
done
