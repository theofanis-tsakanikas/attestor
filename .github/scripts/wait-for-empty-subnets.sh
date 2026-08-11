#!/usr/bin/env bash
# Wait until nothing is left plugged into the private subnets.
#
# AgentCore puts network interfaces into the subnets it is given and takes them back on its
# own schedule — minutes after the runtime, the gateways and the memories all report deleted.
# They are `ela-attach` interfaces whose attachment is owned by `amazon-aws`: the account that
# created them cannot detach them and cannot delete them, and that holds whether or not
# AgentCore still has anything left. Tried, on a teardown where the service reported no
# runtimes, no gateways and no memories:
#
#   DetachNetworkInterface  OperationNotPermitted    not allowed to manage 'ela-attach'
#   DeleteNetworkInterface  InvalidParameterValue    interface is currently in use
#
# A dry run says both would succeed, which is worth knowing about dry runs: `--dry-run` answers
# the authorisation question and nothing else. The deploy role is permitted; the operation is
# refused anyway.
#
# What they can do is hold a subnet open, and `DeleteSubnet` answers `DependencyViolation` for
# as long as they do. Behind the subnet sit the route table and the NAT gateway, so Terraform
# spends the whole run on the three resources in front of the blockage and never reaches the
# one that costs money. Waiting is the only remedy this script has.
#
# The AWS provider retries that for about forty minutes and then fails the run, which is the
# worst of both: the wait happened anyway, and the estate is still there at the end of it.
# Waiting here instead makes the same time productive and leaves the destroy a single pass.
#
# Reads the subnet ids from Terraform state rather than from tags, so a subnet that lost its
# tags is still waited on.

set -euo pipefail

DEADLINE_SECONDS="${SUBNET_WAIT_SECONDS:-1200}"
INTERVAL_SECONDS=30

subnets=$(terraform -chdir=infra/foundation state list 2>/dev/null |
  grep '^aws_subnet\.' |
  while read -r address; do
    terraform -chdir=infra/foundation state show -no-color "$address" 2>/dev/null |
      awk '$1 == "id" { gsub(/"/, "", $3); print $3 }'
  done | tr '\n' ',' | sed 's/,$//')

if [ -z "$subnets" ]; then
  echo "  no subnets in state; nothing to wait for"
  exit 0
fi

echo "  waiting for $subnets to empty"

# Whether an interface is slow or abandoned is answered by the service, not by the clock.
# `GRACE_SECONDS` is how long a genuinely-releasing interface is given before this asks that
# question; `DEADLINE_SECONDS` is how long one AgentCore still owns is waited on.
GRACE_SECONDS="${SUBNET_GRACE_SECONDS:-300}"

agentcore_holds_nothing() {
  [ "$(aws bedrock-agentcore-control list-agent-runtimes --query 'length(agentRuntimes)' \
      --output text 2>/dev/null || echo 1)" = "0" ] &&
    [ "$(aws bedrock-agentcore-control list-gateways --query 'length(items)' \
        --output text 2>/dev/null || echo 1)" = "0" ] &&
    [ "$(aws bedrock-agentcore-control list-memories --query 'length(memories)' \
        --output text 2>/dev/null || echo 1)" = "0" ]
}

waited=0
while [ "$waited" -lt "$DEADLINE_SECONDS" ]; do
  remaining=$(aws ec2 describe-network-interfaces \
    --filters "Name=subnet-id,Values=$subnets" \
    --query 'length(NetworkInterfaces)' --output text)

  if [ "$remaining" = "0" ]; then
    echo "  subnets are empty after ${waited}s"
    exit 0
  fi

  aws ec2 describe-network-interfaces \
    --filters "Name=subnet-id,Values=$subnets" \
    --query 'NetworkInterfaces[].[NetworkInterfaceId,InterfaceType,Status]' --output text |
    sed 's/^/    still attached: /'

  # Past the grace period with the service holding nothing, the remaining time is spent
  # waiting for an owner that no longer exists. Nothing here can shorten that wait — the
  # interfaces are not ours to remove — but it can stop pretending the deadline is the
  # question. Say so once, and stop, so the failure arrives in minutes rather than in an hour.
  if [ "$waited" -ge "$GRACE_SECONDS" ] && agentcore_holds_nothing; then
    echo "  AgentCore holds no runtime, gateway or memory, and these interfaces are still" >&2
    echo "  attached. They are 'ela-attach' and cannot be detached or deleted by this" >&2
    echo "  account; AWS releases them on its own schedule. Re-run the teardown later." >&2
    break
  fi

  sleep "$INTERVAL_SECONDS"
  waited=$((waited + INTERVAL_SECONDS))
done

# Not a failure of its own. The destroy that follows will fail on the DependencyViolation and
# name the resource, which is a better error than "the script gave up".
echo "  subnets are still occupied after ${DEADLINE_SECONDS}s; destroying anyway" >&2
exit 1
