#!/usr/bin/env bash
# The environment the deploy's "run against the live estate" step builds, on a laptop.
#
#   source scripts/live-env.sh && attestor run --tenant helios
#
# The last step of a deploy is `attestor run`, and that command needs no deploy to run. It
# needs six values, all of them Terraform outputs from a standing estate. Reading them here
# turns a twenty-five minute round trip into forty seconds.
#
# That matters more than convenience. Five consecutive deploys were spent discovering one
# narrative defect each — an invented placeholder, a version designation, a guardrail
# refusal — and not one of them touched a line of Terraform. They were Python and prompt
# changes, iterated at the speed of the slowest possible feedback loop for no reason at all.
#
# Read-only. It exports variables and asserts they are non-empty; it does not apply, create or
# delete anything. Deploys still go through CI, because a layer that can be applied from a
# laptop is a layer that will drift.

# Not `set -e`: this file is meant to be sourced, and killing the caller's shell on a missing
# output is a poor way to report one.

: "${AWS_REGION:=eu-central-1}"
export AWS_REGION

# The backend, from the account rather than from this file. `infra/bootstrap` publishes these
# to `/attestor/bootstrap/*` and every workflow reads them there; the deploy and destroy jobs
# have done so from the start. This script used to carry the bucket name as a literal, which
# made it the one place a rename would be missed — and, because the name embeds the account
# id, the one place this repository stated which account it deploys into.
_live_env_param() {
  aws ssm get-parameter --name "/attestor/bootstrap/$1" \
    --query 'Parameter.Value' --output text 2>/dev/null
}
: "${TF_STATE_BUCKET:=$(_live_env_param state_bucket)}"
: "${TF_LOCK_TABLE:=$(_live_env_param lock_table)}"

if [ -z "${TF_STATE_BUCKET:-}" ] || [ -z "${TF_LOCK_TABLE:-}" ]; then
  echo "  cannot read /attestor/bootstrap/* — are you signed in to the right account?" >&2
fi

_live_env_output() {
  local layer="$1" name="$2" value
  terraform -chdir="infra/$layer" init -input=false -reconfigure \
    -backend-config="bucket=$TF_STATE_BUCKET" \
    -backend-config="key=$layer/terraform.tfstate" \
    -backend-config="region=$AWS_REGION" \
    -backend-config="dynamodb_table=$TF_LOCK_TABLE" >/dev/null 2>&1
  value=$(terraform -chdir="infra/$layer" output -raw "$name" 2>/dev/null)
  if [ -z "$value" ]; then
    echo "  MISSING: $layer output '$name' — is the estate standing?" >&2
    return 1
  fi
  printf '%s' "$value"
}

# `athena`, or every one of these is theatre: the run replays `recordings/` and prints PASS
# without touching the account, and the captured prose is published as though a model had just
# written it.
export ATTESTOR_BACKEND=athena
export ATTESTOR_WORKGROUP="${ATTESTOR_WORKGROUP:-attestor}"
export ATTESTOR_DATABASE="${ATTESTOR_DATABASE:-attestor_gold}"

_live_env_lake=$(_live_env_output foundation lake_bucket) &&
  export ATTESTOR_ATHENA_OUTPUT="s3://${_live_env_lake}/athena-results/"
ATTESTOR_EVIDENCE_KB=$(_live_env_output knowledge evidence_kb_id) && export ATTESTOR_EVIDENCE_KB
ATTESTOR_REGULATORY_KB=$(_live_env_output knowledge regulatory_kb_id) && export ATTESTOR_REGULATORY_KB
ATTESTOR_GUARDRAIL_ID=$(_live_env_output knowledge guardrail_id) && export ATTESTOR_GUARDRAIL_ID
ATTESTOR_GUARDRAIL_VER=$(_live_env_output knowledge guardrail_version) && export ATTESTOR_GUARDRAIL_VER
ATTESTOR_REASONING_MODEL=$(_live_env_output agent reasoning_model) && export ATTESTOR_REASONING_MODEL
unset _live_env_lake

for _live_env_name in ATTESTOR_ATHENA_OUTPUT ATTESTOR_EVIDENCE_KB ATTESTOR_REGULATORY_KB \
  ATTESTOR_GUARDRAIL_ID ATTESTOR_GUARDRAIL_VER ATTESTOR_REASONING_MODEL; do
  eval "_live_env_value=\${$_live_env_name:-}"
  if [ -z "$_live_env_value" ]; then
    echo "  $_live_env_name is unset; the estate is not fully standing" >&2
  else
    echo "  $_live_env_name=$_live_env_value"
  fi
done
unset _live_env_name _live_env_value
