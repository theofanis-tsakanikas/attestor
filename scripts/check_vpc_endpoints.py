#!/usr/bin/env python3
"""Everything reached from inside the VPC has a way out of it.

The private subnets have one route to AWS: interface endpoints, plus the S3 gateway. The
security group's egress is the VPC CIDR and nothing else. That is a deliberate and good
design — the agent's traffic stays on the audited path — and it has one failure mode, which
this repository has now hit twice.

A service with no endpoint does not fail. Nothing refuses the connection and nothing raises;
the call waits until something above it gives up.

  - `bedrock-agentcore` had no endpoint, so writing an analyst's question to memory hung. The
    tool had already computed its answer and the caller was told "An internal error occurred"
    after Lambda killed the invocation at 180 seconds.
  - `ecr.api` and `ecr.dkr` had none, so the AgentCore Runtime could not pull the image it is
    made of. The control plane reported it `READY` — the resource existed — while its logs
    said `failed to resolve image ... i/o timeout` every few seconds. It had never started on
    any deploy, and nothing noticed because nothing called it.

Both were found by reading logs after a live call failed. This finds them from the source, with
no credentials, before the estate is stood up.

Two sources of truth are compared:

1. **What the code calls.** Every `boto3.client("service")` in `src/` needs an endpoint, and the
   list is read from the code rather than maintained beside it.
2. **What the platform needs on our behalf.** ECR is the case that proves the point: nothing in
   `src/` mentions it, and without it nothing in `src/` ever runs. Those are declared below,
   each with the reason, because no amount of reading our own code would reveal them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOUNDATION_TF = ROOT / "infra" / "foundation" / "main.tf"
SOURCE = ROOT / "src"

#: Reached by the platform on our behalf, so no reading of our code would ever find them.
PLATFORM_REQUIRED = {
    "ecr.api": "AgentCore Runtime authenticates to ECR before it can pull the agent image",
    "ecr.dkr": "and pulls the image layers from it; without both the runtime never starts",
}

#: Served by the S3 *gateway* endpoint, which is a route-table entry rather than an interface.
#: Athena's results and Iceberg's data both travel this way.
BY_GATEWAY = {"s3"}

#: Called from a runner or a laptop, never from inside the VPC. `cognito-idp` is the deploy
#: authenticating as the verification principal; `bedrock-agentcore-control` is Terraform.
#: `bedrock-data-automation-runtime` is reached by `pipelines/ingest/evidence.py`, which the
#: deploy runs on the CI runner before the estate is even asked for a report. If extraction ever
#: moves inside a tool handler it needs an endpoint like everything else, and moving it without
#: removing this line will make it hang exactly the way memory did.
OUTSIDE_THE_VPC = {
    "cognito-idp",
    "bedrock-agentcore-control",
    "bedrock-data-automation-runtime",
    "glue",
    "ec2",
}

CLIENT_CALL = re.compile(r"""boto3\.client\(\s*["']([a-z0-9.-]+)["']""")


def declared_endpoints() -> set[str]:
    """The interface endpoints `infra/foundation` creates, read from its `for_each`."""
    text = FOUNDATION_TF.read_text(encoding="utf-8")
    match = re.search(
        r'resource\s+"aws_vpc_endpoint"\s+"interface"\s*\{.*?for_each\s*=\s*toset\(\[(.*?)\]\)',
        text,
        re.DOTALL,
    )
    if not match:
        return set()
    return set(re.findall(r'"([a-z0-9.-]+)"', match.group(1)))


def services_the_code_calls() -> dict[str, str]:
    """Every AWS service `src/` opens a client for, and the file that opens it."""
    called: dict[str, str] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        for service in CLIENT_CALL.findall(path.read_text(encoding="utf-8")):
            called.setdefault(service, str(path.relative_to(ROOT)))
    return called


def problems() -> list[str]:
    declared = declared_endpoints()
    if not declared:
        return ["infra/foundation/main.tf declares no interface endpoints; this check read nothing"]

    found: list[str] = []
    for service, where in sorted(services_the_code_calls().items()):
        if service in BY_GATEWAY or service in OUTSIDE_THE_VPC or service in declared:
            continue
        found.append(
            f"{where} opens a client for {service!r} and the VPC has no endpoint for it. "
            "Egress is the VPC and nothing else, so that call will not fail — it will hang "
            "until whatever is above it times out"
        )

    for service, reason in sorted(PLATFORM_REQUIRED.items()):
        if service not in declared:
            found.append(f"no endpoint for {service!r}: {reason}")

    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(f"  {problem}")
    print(f"  checked {len(declared_endpoints())} endpoint(s), {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
