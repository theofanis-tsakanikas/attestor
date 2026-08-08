#!/usr/bin/env python3
"""Every AgentCore surface authenticates against exactly one tenant.

A `CUSTOM_JWT` authorizer validates against one issuer's keys. This estate has one Cognito pool
per tenant, so one issuer per tenant, so a surface that admits more than one tenant's clients is
not multi-tenant — it is single-tenant with a list that says otherwise.

That is not a hypothetical. It is what was deployed. The shared AgentCore Runtime chose its
`discovery_url` with `values(aws_cognito_user_pool.tenant)[0]` — an arbitrary map ordering,
which resolved to `aegis` — and listed every tenant's app client. The consequence was not a
leak: the session's own issuer binding still refused a token that named the wrong undertaking.
It was quieter and in some ways worse. `helios` could not reach the runtime at all, and which
tenant *could* was decided by the order Terraform iterated a map. The comment above the gateway
had described this exact failure since the day the gateway was fixed; the runtime kept the
shape the comment was written about, and nothing looked.

So this looks. Two rules, both readable from the source with no credentials:

1. An authorizer's `discovery_url` names a pool through a per-resource key, never by index.
2. Its `allowed_clients` names one tenant's clients, never a comprehension over all of them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_TF = ROOT / "infra" / "agent" / "main.tf"

#: Picking a pool positionally. Whatever is at index zero is whatever the map iterated first,
#: which is a fact about Terraform's internals and not about which undertaking is being served.
BY_INDEX = re.compile(r"values\(\s*aws_cognito_user_pool\.tenant\s*\)\s*\[\s*\d+\s*\]")

#: Listing every tenant's client against one issuer's keys. The shape that looks multi-tenant
#: and admits one.
ALL_CLIENTS = re.compile(r"for\s+\w+\s+in\s+aws_cognito_user_pool_client\.tenant\s*:")


def authorizer_blocks(terraform: str) -> list[str]:
    """Every `custom_jwt_authorizer = { ... }` body in the file, brace-matched."""
    blocks: list[str] = []
    for match in re.finditer(r"custom_jwt_authorizer\s*=\s*\{", terraform):
        depth, index = 0, match.end() - 1
        for position in range(match.end() - 1, len(terraform)):
            if terraform[position] == "{":
                depth += 1
            elif terraform[position] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(terraform[index : position + 1])
                    break
    return blocks


def problems() -> list[str]:
    terraform = AGENT_TF.read_text(encoding="utf-8")
    blocks = authorizer_blocks(terraform)
    if not blocks:
        return ["infra/agent/main.tf declares no JWT authorizer; this check would pass on nothing"]

    found: list[str] = []
    for number, block in enumerate(blocks, 1):
        if BY_INDEX.search(block):
            found.append(
                f"authorizer {number} picks its pool positionally. Which tenant this surface "
                "serves would be decided by the order Terraform iterates a map"
            )
        if ALL_CLIENTS.search(block):
            found.append(
                f"authorizer {number} admits every tenant's clients against one issuer's keys. "
                "One pool per tenant means one issuer per tenant: this surface serves exactly "
                "one undertaking, and a list that says otherwise locks the others out silently"
            )
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(f"  {problem}", file=sys.stderr)
    print(
        f"  checked {len(authorizer_blocks(AGENT_TF.read_text(encoding='utf-8')))} "
        f"authorizer(s), {len(found)} problem(s)"
    )
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
