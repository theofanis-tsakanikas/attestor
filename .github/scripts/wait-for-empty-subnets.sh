#!/usr/bin/env bash
# Wait until nothing is left plugged into the private subnets.
#
# AgentCore puts network interfaces into the subnets it is given and takes them back on its
# own schedule — minutes after the runtime, the gateways and the memories all report deleted.
# They are `ela-attach` interfaces whose attachment is owned by `amazon-aws`. What they can do
# is hold a subnet open, and `DeleteSubnet` answers `DependencyViolation` for as long as they
# do — and behind the subnet sit the route table and the NAT gateway, so one of these bills
# money for as long as it is forgotten.
#
# This file used to state that they can neither be detached nor deleted. That is true while
# AgentCore still owns them and false once it does not: with the service holding nothing, a
# forced detach and a delete both succeed. The distinction is made below by asking the service
# rather than by assuming either way.
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

# Only interfaces of AgentCore's own type, only inside the subnets this state file owns, and
# only once the service has nothing left that could claim them.
#
# Observed on 11 August 2026: every AgentCore resource in the account reported deleted and two
# `agentic_ai` interfaces stayed `in-use` for over two hours across three teardowns. Nothing
# was coming back for them. They are not slow, they are orphaned — and they hold the private
# subnets, which hold the route table, which holds the NAT gateway. Terraform never even
# reached the NAT: it spent every run failing on the three resources in front of it.
#
# One forgotten interface bills a NAT gateway until somebody looks.
reclaim_orphans() {
  local orphans eni attachment
  orphans=$(aws ec2 describe-network-interfaces \
    --filters "Name=subnet-id,Values=$subnets" "Name=interface-type,Values=agentic_ai" \
    --query 'NetworkInterfaces[].NetworkInterfaceId' --output text)
  [ -n "$orphans" ] || return 1

  for eni in $orphans; do
    echo "  AgentCore holds nothing; reclaiming orphaned $eni"
    attachment=$(aws ec2 describe-network-interfaces --network-interface-ids "$eni" \
      --query 'NetworkInterfaces[0].Attachment.AttachmentId' --output text 2>/dev/null)
    if [ -n "$attachment" ] && [ "$attachment" != "None" ]; then
      aws ec2 detach-network-interface --attachment-id "$attachment" --force 2>&1 |
        sed 's/^/    detach: /' || true
      sleep 15
    fi
    aws ec2 delete-network-interface --network-interface-id "$eni" 2>&1 |
      sed 's/^/    delete: /' || true
  done
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

  # Past the grace period, and the service that made them has nothing left. Waiting out the
  # full deadline here would be waiting for something that is not coming.
  if [ "$waited" -ge "$GRACE_SECONDS" ] && agentcore_holds_nothing && reclaim_orphans; then
    remaining=$(aws ec2 describe-network-interfaces \
      --filters "Name=subnet-id,Values=$subnets" \
      --query 'length(NetworkInterfaces)' --output text)
    if [ "$remaining" = "0" ]; then
      echo "  subnets are empty after reclaiming"
      exit 0
    fi
  fi

  sleep "$INTERVAL_SECONDS"
  waited=$((waited + INTERVAL_SECONDS))
done

# Not a failure of its own. The destroy that follows will fail on the DependencyViolation and
# name the resource, which is a better error than "the script gave up".
echo "  subnets are still occupied after ${DEADLINE_SECONDS}s; destroying anyway" >&2
exit 1
