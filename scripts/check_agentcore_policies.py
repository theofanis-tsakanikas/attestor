#!/usr/bin/env python3
"""The AgentCore policy set still holds the one rule it is in the estate for.

`policy/agentcore/` is small on purpose, and small sets are the ones that quietly become
empty. The engine costs a resource, a role permission and a page of explanation; if the
`forbid` on `request_override` ever goes, what remains is a `permit` that grants what the
gateway already granted — an authorization layer that authorizes everything, which reads on an
architecture diagram exactly like one that does something.

Doctrine rule 2 is why that forbid exists: the system may never open a door for itself. No
model, no agent, no service principal may request, approve or classify an override.

Two other things are checked, and both are about staying honest rather than staying correct:

- every `${...}` placeholder is one `infra/agent` actually substitutes, so a renamed template
  variable fails here instead of deploying a policy with a literal `${gateway_arn}` in it,
- no policy reads `principal.getTag(...)`. Cognito's claim-to-tag mapping has not been
  verified against a live token in this account, and a policy that asserts one is a control
  that is nearly right — which in authorization is the same as wrong. If that mapping is ever
  established, this check is the place to record it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "policy" / "agentcore"
AGENT = ROOT / "infra" / "agent" / "main.tf"

#: The variables `templatefile()` is given in `infra/agent/main.tf`.
SUBSTITUTED = {"gateway_arn", "target"}

PLACEHOLDER = re.compile(r"\$\{(\w+)\}")
POLICY_HEAD = re.compile(r"^\s*(permit|forbid)\s*\(", re.MULTILINE)

#: The rule. Matched on the action rather than on a file name, so renaming the file does not
#: silently satisfy this.
OVERRIDE_FORBID = re.compile(
    r'forbid\s*\([^)]*action\s*==\s*AgentCore::Action::"\$\{target\}___request_override"',
    re.DOTALL,
)


def main() -> int:
    problems: list[str] = []

    if not POLICY_DIR.is_dir():
        print(f"  {POLICY_DIR.relative_to(ROOT)} is missing", file=sys.stderr)
        return 1

    policies = sorted(POLICY_DIR.glob("*.cedar"))
    if not policies:
        problems.append("policy/agentcore/ is empty — the policy engine would grant nothing")

    corpus = ""
    for path in policies:
        text = path.read_text(encoding="utf-8")
        corpus += text
        name = path.name

        heads = len(POLICY_HEAD.findall(text))
        if heads != 1:
            problems.append(
                f"{name}: holds {heads} policies. AgentCore's CreatePolicy takes exactly one, "
                "and reports more as `unexpected token forbid`"
            )

        for placeholder in set(PLACEHOLDER.findall(text)) - SUBSTITUTED:
            problems.append(
                f"{name}: `${{{placeholder}}}` is not one of the values infra/agent "
                f"substitutes ({', '.join(sorted(SUBSTITUTED))}) — it would deploy verbatim"
            )

        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
        if "getTag" in code or "hasTag" in code:
            problems.append(
                f"{name}: reads a token tag. The claim-to-tag mapping has not been verified "
                "in this account; see this file's docstring before asserting one"
            )

    if not OVERRIDE_FORBID.search(corpus):
        problems.append(
            "no policy forbids `request_override` at the gateway. That rule is the reason the "
            "policy engine is in the estate — doctrine rule 2, the door with no key"
        )

    # The template variables have to exist on the Terraform side too, or the substitution
    # silently leaves the placeholder in the deployed statement.
    layer = AGENT.read_text(encoding="utf-8")
    for variable in sorted(SUBSTITUTED):
        if not re.search(rf"^\s*{variable}\s*=", layer, re.MULTILINE):
            problems.append(f"infra/agent does not pass `{variable}` to templatefile()")

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {len(policies)} AgentCore policies, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
