#!/usr/bin/env bash
# Wait until nothing is left plugged into the private subnets.
#
# AgentCore puts network interfaces into the subnets it is given and takes them back on its
# own schedule — minutes after the runtime, the gateways and the memories all report deleted.
# They are `ela-attach` interfaces whose attachment is owned by `amazon-aws`: the account that
# created them cannot detach them and cannot delete them. What they *can* do is hold a subnet
# open, and `DeleteSubnet` answers `DependencyViolation` for as long as they do.
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

  sleep "$INTERVAL_SECONDS"
  waited=$((waited + INTERVAL_SECONDS))
done

# Not a failure of its own. The destroy that follows will fail on the DependencyViolation and
# name the resource, which is a better error than "the script gave up".
echo "  subnets are still occupied after ${DEADLINE_SECONDS}s; destroying anyway" >&2
exit 1
