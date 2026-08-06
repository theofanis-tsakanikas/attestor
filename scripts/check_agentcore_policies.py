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

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "policy" / "agentcore"
AGENT = ROOT / "infra" / "agent" / "main.tf"

#: The variables `templatefile()` is given in `infra/agent/main.tf`.
SUBSTITUTED = {"gateway_arn", "target", "actions"}

#: The MCP tool schema the gateway target is built from.
TOOL_SCHEMA = ROOT / "infra" / "agent" / "tools.openapi.json"

#: Forbidden at the gateway, and therefore not in the permit either — so default-deny holds it
#: shut even if the forbid were removed.
NEVER_PERMITTED = {"request_override"}

PLACEHOLDER = re.compile(r"\$\{(\w+)\}")
POLICY_HEAD = re.compile(r"^\s*(permit|forbid)\s*\(", re.MULTILINE)

#: The rule. Matched on the action rather than on a file name, so renaming the file does not
#: silently satisfy this.
OVERRIDE_FORBID = re.compile(
    r'forbid\s*\([^)]*action\s*==\s*AgentCore::Action::"\$\{target\}___request_override"',
    re.DOTALL,
)


def _permitted_actions_match_the_tools(layer: str) -> list[str]:
    """The permit names every tool the gateway exposes, and no others.

    This is what the Cedar analyzer used to cover. It reported the permit as *Overly
    Permissive* and the forbid as *Overly Restrictive* — both accurate, both the intent
    restated — and `FAIL_ON_ANY_FINDINGS` has no way to say "expected", so it is off. The
    finding worth keeping was the one about a permit that quietly widens, and that is a
    comparison this repository can make itself.

    A tool added to `SPECS` and not to the permit is denied at the gateway. That is the safe
    direction, and it should still be a red build rather than a support ticket.
    """
    if not TOOL_SCHEMA.is_file():
        return [f"{TOOL_SCHEMA.name} is missing; the permitted actions cannot be checked"]

    tools = {tool["name"] for tool in json.loads(TOOL_SCHEMA.read_text(encoding="utf-8"))["tools"]}
    expected = tools - NEVER_PERMITTED

    excluded = re.search(r'tool\.name\s*!=\s*"([^"]+)"', layer)
    if excluded is None:
        return ["infra/agent no longer excludes any tool from the permit"]
    if {excluded.group(1)} != NEVER_PERMITTED:
        return [
            f"infra/agent excludes {excluded.group(1)!r} from the permit; this check expects "
            f"{', '.join(sorted(NEVER_PERMITTED))}"
        ]

    problems = []
    for name in sorted(NEVER_PERMITTED - tools):
        problems.append(
            f"`{name}` is excluded from the permit but is not a tool any more — the exclusion "
            "now protects nothing, and NEVER_PERMITTED should say so"
        )
    if not expected:
        problems.append("the permit would grant no actions at all, so the gateway serves nothing")
    return problems


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

    # The template variables have to be passed on the Terraform side too, or the substitution
    # silently leaves the placeholder in the deployed statement.
    #
    # Read out of the `templatefile(...)` call and not the file at large: `actions` also names
    # a local three lines above, so a search over the whole layer found the definition and
    # called it a use — passing while the argument was gone.
    layer = AGENT.read_text(encoding="utf-8")
    call = re.search(r"templatefile\(.*?\{(?P<args>.*?)\}\s*\)", layer, re.DOTALL)
    if call is None:
        problems.append("infra/agent has no templatefile() call for the AgentCore policies")
    else:
        passed = set(re.findall(r"^\s*(\w+)\s*=", call.group("args"), re.MULTILINE))
        for variable in sorted(SUBSTITUTED - passed):
            problems.append(f"infra/agent does not pass `{variable}` to templatefile()")

    problems.extend(_permitted_actions_match_the_tools(layer))

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {len(policies)} AgentCore policies, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
