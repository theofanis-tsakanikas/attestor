#!/usr/bin/env bash
# One place that knows how a layer is initialised and applied.
#
# Inlining this in the workflow meant repeating the backend configuration for every layer,
# and a backend block that differs by one line between two steps is a second state file
# nobody notices until a destroy leaves half an estate standing.
set -euo pipefail

action="${1:?apply|plan|destroy}"
layer="${2:?layer name}"

terraform -chdir="infra/${layer}" init \
  -backend-config="bucket=${TF_STATE_BUCKET}" \
  -backend-config="region=${AWS_REGION}" \
  -backend-config="dynamodb_table=${TF_LOCK_TABLE}" \
  -backend-config="encrypt=true"

export TF_VAR_state_bucket="${TF_STATE_BUCKET}"
export TF_VAR_region="${AWS_REGION}"
export TF_VAR_deploy_role_arn="${AWS_DEPLOY_ROLE_ARN:-}"

case "$action" in
  apply)   terraform -chdir="infra/${layer}" apply -auto-approve -input=false ;;
  plan)    terraform -chdir="infra/${layer}" plan -input=false ;;
  destroy) terraform -chdir="infra/${layer}" destroy -auto-approve -input=false ;;
  *) echo "unknown action $action" >&2; exit 2 ;;
esac
