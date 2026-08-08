#!/usr/bin/env python3
"""The two surfaces serving the same tools may do the same things.

`attestor-tools` (the Lambda behind the Gateway) and the AgentCore Runtime run identical code:
the same six handlers, the same resolver, the same Athena queries, the same memory writes. They
have separate IAM roles, because they are separate principals, and separate roles drift.

They did. Only the Lambda had ever been called, so only the Lambda's role had been corrected —
twice, both times for Athena, both times found by a live failure:

  - `s3:ListBucket` and `s3:GetBucketLocation` were missing from the tools role, and
    `StartQueryExecution` failed in 809 ms with `E_RESOLVER_ERROR`. Athena resolves a table's
    location and prepares its result set before it runs anything.
  - The runtime role then failed the same way, with the same missing action, on the first call
    ever made to it: `Unable to verify/create output bucket`.

A role that grants less than the code needs is not a tighter role. It is the same code, refusing
to answer, in a way that reads as a data problem.

This compares the two policies' data-plane actions and requires the runtime's to cover the
Lambda's. Not equality: the Lambda has `logs:*` from its VPC execution role and the runtime has
`ecr:*` to pull its image, and neither belongs to the other. What both must have is everything
needed to resolve a datapoint and record that they did.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_TF = ROOT / "infra" / "agent" / "main.tf"

#: The services a tool call touches on its way to an answer. Anything outside these is the
#: business of one surface only — pulling an image, writing its own log stream — and is not
#: something the other is missing.
SHARED_SERVICES = ("athena", "glue", "s3", "bedrock", "bedrock-agentcore", "kms")

ACTION = re.compile(r'"((?:' + "|".join(SHARED_SERVICES) + r')[:][A-Za-z*]+)"')


def policy_body(name: str, terraform: str) -> str:
    """The `aws_iam_role_policy` block with this name, brace-matched from its resource header."""
    match = re.search(rf'resource\s+"aws_iam_role_policy"\s+"{name}"\s*\{{', terraform)
    if not match:
        return ""
    depth, start = 0, match.end() - 1
    for position in range(match.end() - 1, len(terraform)):
        if terraform[position] == "{":
            depth += 1
        elif terraform[position] == "}":
            depth -= 1
            if depth == 0:
                return terraform[start : position + 1]
    return ""


def problems() -> list[str]:
    terraform = AGENT_TF.read_text(encoding="utf-8")
    tools, runtime = policy_body("tools", terraform), policy_body("runtime", terraform)
    if not tools or not runtime:
        return ["could not find both the tools and runtime role policies; this check read nothing"]

    granted_to_tools = set(ACTION.findall(tools))
    granted_to_runtime = set(ACTION.findall(runtime))
    if not granted_to_tools:
        return ["the tools role grants none of the shared services; the pattern has gone stale"]

    missing = sorted(granted_to_tools - granted_to_runtime)
    if missing:
        return [
            "the runtime role is missing "
            + ", ".join(missing)
            + ". It runs the same handlers as the Lambda, so it will fail on the same call — "
            "and the failure will arrive as an abstention, which reads like a data problem"
        ]
    return []


def main() -> int:
    found = problems()
    for problem in found:
        print(f"  {problem}")
    terraform = AGENT_TF.read_text(encoding="utf-8")
    shared = len(set(ACTION.findall(policy_body("tools", terraform))))
    print(f"  compared {shared} shared action(s), {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
