#!/usr/bin/env bash
# Attach the tool handlers to a gateway, or detach them.
#
# WHY THIS IS NOT A TERRAFORM RESOURCE
#
# There is no gateway-target resource in `hashicorp/awscc` (checked at 1.95.0, the latest
# published version) and none in `hashicorp/aws`. The control-plane API exists; the provider
# coverage does not. `scripts/check_gateway_target.py` re-checks that on every CI run and
# fails once a provider ships one, so this script is deleted by a failing test rather than by
# somebody remembering.
#
# It is driven by a `null_resource` in main.tf rather than by a workflow step, deliberately:
# that keeps the target inside the dependency graph, inside `terraform apply`, and inside
# `terraform destroy`. A workflow step would leave an orphaned target behind on teardown, and
# an orphaned target on a deleted gateway is the kind of thing nobody finds until the bill.
#
# Idempotent by construction: it looks the target up by name and updates rather than creates
# when it is already there, because a provisioner runs again whenever its trigger changes.
set -euo pipefail

# Dependencies, checked before anything is attempted. `bedrock-agentcore-control` is a recent
# service: an AWS CLI too old to know it fails with "Invalid choice", which reads as a broken
# script rather than a stale toolchain — and it fails *after* the expensive layers are already
# standing. Better to say so in one line.
for binary in aws jq; do
  command -v "$binary" >/dev/null || { echo "$binary is not installed" >&2; exit 2; }
done
aws bedrock-agentcore-control help >/dev/null 2>&1 || {
  echo "this AWS CLI does not know 'bedrock-agentcore-control' (aws --version: $(aws --version 2>&1))." >&2
  echo "Upgrade to a build that includes it; the gateway target cannot be attached without it." >&2
  exit 2
}

action="${1:?attach|detach}"
gateway_id="${2:?gateway identifier}"
region="${3:?region}"
lambda_arn="${4-}"
schema_file="${5-}"
name="attestor-tools"

exists() {
  aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$gateway_id" --region "$region" \
    --query "items[?name=='${name}'].targetId | [0]" --output text 2>/dev/null \
    | grep -v '^None$' || true
}

case "$action" in
  attach)
    [ -n "$lambda_arn" ] || { echo "attach needs a lambda arn" >&2; exit 2; }
    [ -r "$schema_file" ] || { echo "cannot read $schema_file" >&2; exit 2; }

    # The schema is generated from `tools.SPECS` by `attestor gateway spec`. Building the
    # payload here from anything else would be a second description of one contract.
    payload=$(jq -c --arg arn "$lambda_arn" \
      '{mcp: {lambda: {lambdaArn: $arn, toolSchema: {inlinePayload: .tools}}}}' \
      "$schema_file")
    credentials='[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]'

    target_id=$(exists)
    if [ -n "$target_id" ]; then
      echo "updating target $target_id on $gateway_id"
      aws bedrock-agentcore-control update-gateway-target \
        --gateway-identifier "$gateway_id" --target-id "$target_id" --region "$region" \
        --name "$name" \
        --description "The six tool handlers, as MCP operations." \
        --target-configuration "$payload" \
        --credential-provider-configurations "$credentials" >/dev/null
    else
      echo "creating target on $gateway_id"
      aws bedrock-agentcore-control create-gateway-target \
        --gateway-identifier "$gateway_id" --region "$region" \
        --name "$name" \
        --description "The six tool handlers, as MCP operations." \
        --target-configuration "$payload" \
        --credential-provider-configurations "$credentials" >/dev/null
    fi
    ;;

  detach)
    target_id=$(exists)
    if [ -n "$target_id" ]; then
      echo "deleting target $target_id from $gateway_id"
      aws bedrock-agentcore-control delete-gateway-target \
        --gateway-identifier "$gateway_id" --target-id "$target_id" --region "$region" >/dev/null
    fi
    ;;

  *)
    echo "unknown action $action" >&2
    exit 2
    ;;
esac
