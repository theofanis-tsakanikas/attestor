#!/usr/bin/env python3
"""The policies Terraform deploys are the policies the offline evaluator was tested against.

`infra/agent` cannot hand AgentCore a `.cedar` file. `CreatePolicy` accepts a single policy
statement, and a file containing four of them fails on `unexpected token forbid` — the parser
reads the first `permit`, reaches its end, and finds another policy where it expected
end-of-input. So the layer splits each file with a regex over `@id(...)` annotations, and one
AgentCore policy is created per Cedar policy.

That regex is a second parser for the same files. `src/attestor/policy/cedar.py` is the first,
and it is the one every test, every eval and the whole isolation suite exercise. Two parsers
over one input drift, and the drift here is not cosmetic: a policy the regex fails to extract
is a `forbid` that exists in the repository, passes claim 2 offline, and is simply absent from
the deployed engine. Nothing goes red. The estate just enforces less than the tests proved.

So this compares them — same ids, same count, and every deployed statement still a single
policy — and fails the build when they disagree.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CEDAR_DIR = ROOT / "policy" / "cedar"
AGENT = ROOT / "infra" / "agent" / "main.tf"

#: Read out of `infra/agent/main.tf` rather than copied, so editing the layer's regex without
#: editing this file cannot leave the check validating a pattern nobody deploys.
TERRAFORM_REGEX = re.compile(r'regexall\(\s*"(?P<pattern>.+?)",', re.DOTALL)

#: A statement holding more than one policy is what AgentCore rejects, and the cheapest way to
#: see it is to count policy heads.
POLICY_HEAD = re.compile(r"^\s*(?:permit|forbid)\s*\(", re.MULTILINE)


def terraform_pattern() -> str | None:
    """The pattern `infra/agent` actually uses, translated from HCL's escaping to Python's."""
    match = TERRAFORM_REGEX.search(AGENT.read_text(encoding="utf-8"))
    if match is None:
        return None
    return match.group("pattern").replace("\\\\", "\\").replace('\\"', '"')


def extract() -> tuple[dict[str, str], list[str]]:
    """The `local.cedar_policies` map, computed the way Terraform computes it.

    Exported because `check_agentcore_names.py` needs the same keys — those are the AgentCore
    policy names — and two scripts deriving them separately is the drift this file exists to
    catch, reintroduced one directory over.
    """
    problems: list[str] = []
    pattern = terraform_pattern()
    if pattern is None:
        return {}, ["no regexall(...) found in infra/agent/main.tf"]

    extracted: dict[str, str] = {}
    for path in sorted(CEDAR_DIR.rglob("*.cedar")):
        for name, body in re.findall(pattern, path.read_text(encoding="utf-8")):
            key = name.replace("-", "_")
            if key in extracted:
                problems.append(
                    f"{name}: two policies share this @id, so one would overwrite the other"
                )
            extracted[key] = body.strip()
    return extracted, problems


def main() -> int:
    extracted, problems = extract()
    if not extracted and problems:
        print(f"  {problems[0]}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(ROOT / "src"))
    from attestor.policy.cedar import load  # noqa: PLC0415 - after sys.path is arranged

    loaded = {policy_id.replace("-", "_") for policy_id in load(ROOT).ids}

    missing = sorted(loaded - set(extracted))
    if missing:
        problems.append(
            f"the evaluator loads {', '.join(missing)} and Terraform does not extract them — "
            "they would pass every offline check and never reach the deployed engine"
        )
    extra = sorted(set(extracted) - loaded)
    if extra:
        problems.append(
            f"Terraform extracts {', '.join(extra)} and the evaluator does not load them — "
            "the estate would enforce a policy nothing here has tested"
        )

    for name, body in sorted(extracted.items()):
        heads = len(POLICY_HEAD.findall(body))
        if heads != 1:
            problems.append(
                f"{name}: the extracted statement holds {heads} policies, and AgentCore "
                "accepts exactly one — this is the failure the split exists to prevent"
            )
        if "@id" in body:
            problems.append(f"{name}: the annotation survived the split; AgentCore rejects `@`")

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {len(extracted)} Cedar policies, {len(loaded)} loaded, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
