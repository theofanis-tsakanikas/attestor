#!/usr/bin/env python3
"""Terraform and the agent code must agree on every name they pass between them.

This check exists because of one line. `tenants/helios.yaml` declared

    issuer: https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_EXAMPLE

nothing replaced it at deploy time, and `Session._check_provider` compares a token's `iss`
against it. So every real Cognito token presented to the gateway was refused by our own
handler — the AgentCore path could not have worked on any deploy that has ever run. Nobody
found out because nothing ever called it, and no test could have: offline there is no pool, so
the placeholder is correct offline and wrong everywhere else.

That is the shape of the whole class. The code reads `ATTESTOR_ISSUER_HELIOS`; Terraform
builds a map key `ATTESTOR_ISSUER_${upper(tenant)}`. They are in different files, in different
languages, and nothing connects them but a naming convention two people have to keep in their
heads. This is the thing that keeps it.

Reads the Terraform source rather than a plan, so it runs with no credentials, on a laptop,
like every other gate here.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_TF = ROOT / "infra" / "agent" / "main.tf"
TENANTS = ROOT / "tenants"

#: Every prefix the agent code resolves from the environment, and the module that reads it.
#: A new one is added here at the same time it is added there, or this fails.
RESOLVED_PREFIXES = {
    "ATTESTOR_ISSUER": "src/attestor/policy/tenants.py",
    "ATTESTOR_AUDIENCE": "src/attestor/policy/tenants.py",
    "ATTESTOR_MEMORY": "src/attestor/agent/memory.py",
    "ATTESTOR_GATEWAY_ROLE": "src/attestor/agent/handler.py",
}

#: The Lambda and the AgentCore runtime. Both serve these tools; an identity that resolved
#: through one door and not the other is worse than neither, because it works in testing.
IDENTITY_SURFACES = 2

#: A committed identity value that must never look real. If one of these stops being obviously
#: a placeholder, someone will read it as configuration and the silent failure returns.
PLACEHOLDER_MARKERS = ("PLACEHOLDER", "EXAMPLE")


def problems() -> list[str]:
    found: list[str] = []
    terraform = AGENT_TF.read_text(encoding="utf-8")

    # 1. Terraform builds a value for every prefix the code will look for.
    for prefix, module in sorted(RESOLVED_PREFIXES.items()):
        key = f'"{prefix}_${{upper(tenant)}}"'
        if key not in terraform:
            found.append(
                f"{module} resolves {prefix}_<TENANT> from the environment, and "
                f"infra/agent/main.tf never builds {key}. The deployed handler would fall "
                "back to the committed placeholder and refuse every real token"
            )

    # 2. The code still reads each of them. A prefix dropped from the code and left in
    #    Terraform is a variable nothing consumes, which is the same drift facing the other way.
    for prefix, module in sorted(RESOLVED_PREFIXES.items()):
        source = (ROOT / module).read_text(encoding="utf-8")
        if f'f"{prefix}_' not in source and f"{prefix}_" not in source:
            found.append(f"{module} no longer reads {prefix}_<TENANT>, but Terraform sets it")

    # 3. Both surfaces receive the merged map. The Lambda and the Runtime serve the same tools;
    #    an identity that resolved through one door and not the other is worse than neither.
    surfaces = terraform.count("merge(local.tenant_identity_env, {")
    if surfaces < IDENTITY_SURFACES:
        found.append(
            f"only {surfaces} surface(s) receive local.tenant_identity_env; the Lambda and the "
            "AgentCore runtime both serve these tools and both must resolve the same identity"
        )

    # 4. Committed identity values look like placeholders, so nobody trusts one.
    for path in sorted(TENANTS.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("issuer:") or "cognito-idp" not in stripped:
                continue
            if not any(marker in stripped for marker in PLACEHOLDER_MARKERS):
                found.append(
                    f"{path.relative_to(ROOT)} declares a Cognito issuer that does not say it "
                    "is a placeholder. The real one contains a pool id Terraform generates; a "
                    "value that reads as real here is the defect this check was written for"
                )
    return found


def main() -> int:
    found = problems()
    for problem in found:
        print(f"  {problem}", file=sys.stderr)
    print(f"  checked {len(RESOLVED_PREFIXES)} resolved prefix(es), {len(found)} problem(s)")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
