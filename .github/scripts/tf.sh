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
  destroy)
    # A layer that was never applied has nothing to destroy — and trying anyway is not a
    # harmless no-op. `terraform destroy` refreshes first, which evaluates this layer's
    # cross-layer `data` sources; when the producing layer was never stood up, those
    # parameters do not exist and the destroy *errors*. In a teardown loop under
    # `set -e` that aborts everything after it, so a partial deploy (`--stage foundation`)
    # would leave the whole estate standing behind a failure about a missing SSM parameter.
    #
    # An empty state is the precise test for "never applied", so it is the one used. A layer
    # that *does* have state and refuses to go still fails, loudly, as it must.
    if ! terraform -chdir="infra/${layer}" state list >/dev/null 2>&1 \
      || [ -z "$(terraform -chdir="infra/${layer}" state list 2>/dev/null)" ]; then
      echo "infra/${layer}: no state, nothing to destroy"
      exit 0
    fi
    terraform -chdir="infra/${layer}" destroy -auto-approve -input=false
    ;;
  *) echo "unknown action $action" >&2; exit 2 ;;
esac
