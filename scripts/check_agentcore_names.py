#!/usr/bin/env python3
"""Every AgentCore resource name is one AgentCore will accept.

`terraform validate` cannot see this. Provider attribute validators run in the plan phase, a
plan needs a backend and credentials, and an offline run reaches neither — so a name AWS
rejects passes every check in this repository and fails a deploy fifteen minutes in, after
OpenSearch Serverless is already standing and metered. That happened twice in a row, on two
resources, because the second was behind the first in the dependency graph and Terraform
never walked far enough to complain about it.

There is no single rule, and assuming there was cost another deploy. Policy Engine and Policy
require `^[A-Za-z][A-Za-z0-9_]*$` — underscores, never hyphens. Gateway requires
`^([0-9a-zA-Z][-]?){1,100}$` — hyphens, never underscores. **No name satisfies both.** So the
table below is per resource kind, and a house style for the layer is not available.

Each entry says where its pattern came from. `verified` means the provider rejected a real
name and printed that regex; `inferred` means names of that shape were accepted in a plan,
which shows the pattern admits them and not where its boundary is. A resource kind with no
entry is reported as unchecked rather than assumed fine — adding one should mean looking the
rule up, not discovering it fifteen minutes into a deploy.

Hard-coding patterns AWS owns is a real cost, and the alternatives were worse. The provider
schema Terraform exposes carries types and not validators, and reading the CloudFormation
resource schema needs the credentials this check exists to avoid.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "infra" / "agent" / "main.tf"
PROJECT = "attestor"

#: resource kind → (pattern AgentCore enforces, where that pattern came from).
#:
#: `verified` entries are quoted from the provider's own rejection message. `inferred` entries
#: are patterns consistent with names that were accepted in a plan — enough to catch a name
#: shaped like the ones that failed, not enough to claim the boundary is exactly here.
PATTERNS: dict[str, tuple[str, str]] = {
    "awscc_bedrockagentcore_policy_engine": (r"^[A-Za-z][A-Za-z0-9_]*$", "verified"),
    "awscc_bedrockagentcore_policy": (r"^[A-Za-z][A-Za-z0-9_]*$", "verified"),
    "awscc_bedrockagentcore_gateway": (r"^([0-9a-zA-Z][-]?){1,100}$", "verified"),
    "awscc_bedrockagentcore_memory": (r"^[A-Za-z][A-Za-z0-9_]*$", "inferred"),
    "awscc_bedrockagentcore_runtime": (r"^[A-Za-z][A-Za-z0-9_]*$", "inferred"),
    "awscc_bedrockagentcore_runtime_endpoint": (r"^[A-Za-z][A-Za-z0-9_]*$", "inferred"),
    "awscc_bedrockagentcore_workload_identity": (r"^[A-Za-z][A-Za-z0-9_-]*$", "inferred"),
}

#: How far a `for_each` may point at another resource's `for_each` before we stop
#: following. Nothing here is more than one hop; the bound exists so a cycle in the
#: Terraform cannot become a recursion in the checker.
MAX_FOR_EACH_HOPS = 3

#: `resource "awscc_bedrockagentcore_<kind>" "<label>" {` and the attribute that names it.
RESOURCE = re.compile(r'^resource\s+"(awscc_bedrockagentcore_\w+)"\s+"(\w+)"\s*\{', re.MULTILINE)
NAME_ATTRIBUTE = re.compile(r"^\s*(?:name|agent_runtime_name)\s*=\s*(.+?)\s*$", re.MULTILINE)
FOR_EACH = re.compile(r"^\s*for_each\s*=\s*(.+?)\s*$", re.MULTILINE)
FILESET = re.compile(r'fileset\(\s*"(.+?)"\s*,\s*"(.+?)"\s*\)')
TOSET_VAR = re.compile(r"toset\(\s*var\.(\w+)\s*\)")


def _from_fileset(for_each: str) -> list[str] | None:
    """`fileset("<dir>", "<glob>")` — the real files on disk."""
    match = FILESET.search(for_each)
    if not match:
        return None
    directory = ROOT / re.sub(r"^\$\{path\.root\}/\.\./\.\./", "", match.group(1))
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.glob(match.group(2)))


def _from_variable(for_each: str) -> list[str] | None:
    """`toset(var.<name>)` — the variable's declared default."""
    match = TOSET_VAR.search(for_each)
    if not match:
        return None
    declared = (ROOT / "infra" / "agent" / "variables.tf").read_text(encoding="utf-8")
    block = re.search(
        rf'variable\s+"{match.group(1)}"\s*\{{(.+?)^\}}', declared, re.DOTALL | re.MULTILINE
    )
    if not block:
        return []
    default = re.search(r"default\s*=\s*\[(.+?)\]", block.group(1), re.DOTALL)
    return re.findall(r'"([^"]+)"', default.group(1)) if default else []


def _from_agentcore_policies(for_each: str) -> list[str] | None:
    """`local.agentcore_policies` — one policy per template, per gateway.

    The keys are `<template>_<tenant>`, and they are the AgentCore policy names. Built from the
    same two inputs the layer uses: the files in `policy/agentcore/` and `cognito_tenants`,
    which is what the gateways iterate — `lumen` has no pool and therefore no gateway.
    """
    if for_each.strip() != "local.agentcore_policies":
        return None
    templates = sorted(
        path.stem.replace("-", "_") for path in (ROOT / "policy" / "agentcore").glob("*.cedar")
    )
    tenants = _from_variable("toset(var.cognito_tenants)") or []
    return [f"{template}_{tenant}" for tenant in tenants for template in templates]


def _from_resource(for_each: str, text: str, depth: int) -> list[str] | None:
    """`aws_cognito_user_pool.tenant` — one resource iterating another's instances.

    Followed rather than given up on, because the alternative is a gateway name nobody checks.
    """
    reference = re.fullmatch(r"(\w+)\.(\w+)", for_each.strip())
    if not reference or depth >= MAX_FOR_EACH_HOPS:
        return None
    block = re.search(
        rf'resource\s+"{reference.group(1)}"\s+"{reference.group(2)}"\s*\{{(.+?)^\}}',
        text,
        re.DOTALL | re.MULTILINE,
    )
    if not block:
        return None
    inner = FOR_EACH.search(block.group(1))
    return iteration_values(inner.group(1), text, depth + 1) if inner else None


def iteration_values(for_each: str | None, text: str, depth: int = 0) -> list[str]:
    """What `each.key` and `each.value` will actually be.

    Real values, not a placeholder. A stand-in of `"helios"` made this check pass on
    `replace(trimsuffix(each.value, ".cedar"), "_", "-")` — the exact expression that broke a
    deploy — because a name with no underscore in it survives a rule about underscores. A
    check fed convenient inputs is a check that agrees with you.

    An unrecognised `for_each` yields nothing, and the caller reports that as a name left
    unchecked rather than as a name that passed.
    """
    if not for_each:
        return [""]
    for resolver in (
        lambda: _from_agentcore_policies(for_each),
        lambda: _from_fileset(for_each),
        lambda: _from_variable(for_each),
        lambda: _from_resource(for_each, text, depth),
    ):
        values = resolver()
        if values is not None:
            return values
    return []


def resolve(expression: str, each: str = "") -> str | None:
    """Reduce a Terraform expression to the literal it will produce, or None if we cannot.

    Deliberately narrow. `replace(...)`, `trimsuffix(...)` and bare interpolation cover every
    name in this layer; anything else returns None and is reported as unreadable rather than
    quietly assumed fine. A check that guesses is a check that passes when it should not.
    """
    expression = expression.strip()

    # Greedy on the first argument, anchored on the trailing string literals. Non-greedy
    # stops at the first comma, which is inside the nested call whenever these are composed —
    # and then the check reports "cannot resolve" for a name whose real problem is the
    # pattern. A gate that fails for the wrong reason is barely better than one that passes.
    trim = re.fullmatch(r'trimsuffix\((.+),\s*"([^"]*)"\)', expression)
    if trim:
        inner = resolve(trim.group(1), each)
        return None if inner is None else inner.removesuffix(trim.group(2))

    replace = re.fullmatch(r'replace\((.+),\s*"([^"]*)",\s*"([^"]*)"\)', expression)
    if replace:
        inner = resolve(replace.group(1), each)
        return None if inner is None else inner.replace(replace.group(2), replace.group(3))

    substitutions = {"var.project": PROJECT, "each.key": each, "each.value": each}

    if expression.startswith('"') and expression.endswith('"'):
        literal = expression[1:-1]
        for reference, value in substitutions.items():
            literal = literal.replace("${" + reference + "}", value)
        return None if "${" in literal else literal

    return substitutions.get(expression)


def blocks(text: str) -> list[tuple[str, str, str, str | None]]:
    found = []
    matches = list(RESOURCE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        # Stop at the closing brace of this resource so a later block's name is not read here.
        closing = re.search(r"^\}", body, re.MULTILINE)
        if closing:
            body = body[: closing.start()]
        attribute = NAME_ATTRIBUTE.search(body)
        if attribute:
            for_each = FOR_EACH.search(body)
            found.append(
                (
                    match.group(1),
                    match.group(2),
                    attribute.group(1),
                    for_each.group(1) if for_each else None,
                )
            )
    return found


def main() -> int:
    if not AGENT.is_file():
        print("no infra/agent/main.tf", file=sys.stderr)
        return 1

    text = AGENT.read_text(encoding="utf-8")
    named = blocks(text)

    problems: list[str] = []
    if not named:
        problems.append("no awscc_bedrockagentcore resources found — this check lost its target")

    checked = 0
    for kind, label, expression, for_each in named:
        if kind not in PATTERNS:
            problems.append(
                f"{kind}.{label}: no pattern recorded for this resource kind, so its name is "
                "unchecked. Look the rule up and add it to PATTERNS with its provenance"
            )
            continue
        pattern, provenance = PATTERNS[kind]
        accepted = re.compile(pattern)
        values = iteration_values(for_each, text)
        if not values:
            problems.append(
                f"{kind}.{label}: cannot enumerate {for_each!r}, so its names go unchecked. "
                "Extend `iteration_values()` rather than leaving them unchecked"
            )
            continue
        for each in values:
            checked += 1
            resolved = resolve(expression, each)
            if resolved is None:
                problems.append(
                    f"{kind}.{label}: cannot resolve {expression!r} to a literal. Extend "
                    "`resolve()` rather than leaving the name unchecked"
                )
            elif not accepted.match(resolved):
                problems.append(
                    f"{kind}.{label}: {resolved!r} does not match {pattern} ({provenance}). "
                    "AgentCore rejects it at apply, which `terraform validate` cannot see"
                )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"  {checked} AgentCore name(s) checked, {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
