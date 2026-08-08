#!/usr/bin/env python3
"""Break every gate on purpose, and require the real gate to refuse it.

A test suite tells you the code does what it does. It does not tell you the *gates* still
bite, because a gate that has quietly stopped checking anything passes every test it has.
This script plants a genuine violation and demands a refusal.

Three rules keep it a proof rather than a ritual:

**Green first.** Every mutation runs against a repository that currently passes. A refusal
from an already-broken baseline proves nothing.

**A non-zero exit is not evidence.** The *named* check must be the thing that failed, with a
message that names the violation. A mutation that happens to cause an unrelated crash is
reported as inconclusive, not as a pass — otherwise the day a gate is deleted, its mutation
still "passes" because the import now fails.

**A mutation whose target has moved is STALE.** If the code a mutation edits no longer looks
the way it expects, the mutation is not silently skipped and is not counted as a pass. It is
reported, and the run is red, because a proof that quietly stopped running is worse than one
that never existed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    gate: str
    #: The command that must fail, run inside the mutated copy.
    command: list[str]
    #: A phrase the failure must contain. Presence of this is what makes the refusal *the*
    #: refusal rather than any old error.
    expect: str
    apply: Callable[[Path], bool]
    #: Why this mutation is worth planting — usually because it is a mistake somebody could
    #: plausibly make, not an absurd one.
    rationale: str


def _replace(path: Path, old: str, new: str) -> bool:
    """Edit a file, returning False if the target text is not there any more."""
    text = path.read_text(encoding="utf-8")
    if old not in text:
        return False
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True


# ── The mutations ────────────────────────────────────────────────────────────


def _launder_a_resolver_error(root: Path) -> bool:
    """Make a crashed resolver look like a lawful omission — the exact laundering ADR-0001
    exists to prevent, and a one-word diff."""
    return _replace(
        root / "src/attestor/contracts/reason_codes.py",
        'code="E_RESOLVER_ERROR",\n        disposition=Disposition.INTERNAL_FAILURE,',
        'code="E_RESOLVER_ERROR",\n        disposition=Disposition.LAWFUL_OMISSION,',
    )


def _let_a_model_write_a_number(root: Path) -> bool:
    """Allow digits in model-authored prose. Somebody would do this to fix a 'false positive'
    on a citation marker."""
    return _replace(
        root / "src/attestor/documents/manifest.py",
        "        return self is not RunKind.NARRATIVE",
        "        return True",
    )


def _drop_the_cross_tenant_forbid(root: Path) -> bool:
    """Delete the policy that separates tenants."""
    path = root / "policy/cedar/tenant_isolation.cedar"
    text = path.read_text(encoding="utf-8")
    marker = '@id("forbid-cross-tenant")'
    if marker not in text:
        return False
    start = text.index(marker)
    end = text.index("};", start) + 2
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    return True


def _put_the_tenant_in_the_cache_key_last(root: Path) -> bool:
    """Remove the tenant from the cache digest. The classic multi-tenant leak, and it looks
    like a harmless refactor."""
    return _replace(
        root / "src/attestor/agent/cache.py",
        '            "tenant": self.tenant,\n            "period": self.period,',
        '            "period": self.period,',
    )


def _widen_the_retrieval_filter(root: Path) -> bool:
    """Let a caller override the session's tenant in a retrieval filter."""
    return _replace(
        root / "src/attestor/retrieval/kb.py",
        "        if key in base and base[key] != value:",
        "        if False:",
    )


def _soften_an_injection_rule(root: Path) -> bool:
    """Remove the imperative anchor, which is how the rules stop distinguishing an attack
    from a document that merely discusses one."""
    return _replace(
        root / "src/attestor/security/injection.py",
        '_IMPERATIVE + r"(?:ignore|disregard|forget|override|bypass)',
        'r"\\b(?:ignore|disregard|forget|override|bypass)',
    )


def _accept_a_dimension_mismatch(root: Path) -> bool:
    """Stop checking that a derived figure's unit matches what its expression produces."""
    return _replace(
        root / "src/attestor/contracts/loader.py",
        "    if actual != expected:",
        "    if False:",
    )


def _let_a_contract_preauthorize_a_crash(root: Path) -> bool:
    """Allow a contract to declare an internal failure as an acceptable omission."""
    return _replace(
        root / "src/attestor/contracts/model.py",
        "            if not resolved.is_lawful:",
        "            if False:",
    )


def _let_an_agent_approve_an_override(root: Path) -> bool:
    """Delete the forbid that stops an override being signed through the agent."""
    path = root / "policy/cedar/roles.cedar"
    text = path.read_text(encoding="utf-8")
    marker = '@id("forbid-approval-through-the-agent")'
    if marker not in text:
        return False
    start = text.index(marker)
    # This policy has no condition block, so it closes with `);` rather than `};`.
    end = text.index(");", start) + 2
    path.write_text(text[:start] + text[end:], encoding="utf-8")
    return True


def _publish_despite_a_blocker(root: Path) -> bool:
    """Render a report that has blockers — a draft with a hole in it, which is what somebody
    reaches for the night before a deadline."""
    return _replace(
        root / "src/attestor/documents/render.py",
        "    if not results.can_issue:\n        raise ReportBlocked(results.blockers)",
        "    if False:\n        raise ReportBlocked(results.blockers)",
    )


def _make_a_policy_unparseable(root: Path) -> bool:
    """Leave the last forbid in the file unterminated.

    Deleting a policy is the obvious attack and `_drop_the_cross_tenant_forbid` covers it.
    This is the quiet one: the forbid is still *in the file*, so a reviewer reading the
    directory sees the retrieval filter exactly where they expect it — and the parser used to
    skip it, load the rest, and report a healthy policy set one control short.

    The *last* policy on purpose. A missing semicolon in the middle of a file makes the next
    policy's text run into the broken one, which fails in the scope parser; the last one has
    nothing after it to run into, so what catches it is the residue check itself.
    """
    return _replace(
        root / "policy/cedar/tenant_isolation.cedar",
        "    context.filter_tenant == principal.tenant\n};",
        "    context.filter_tenant == principal.tenant\n}",
    )


def _drop_the_issuer_binding(root: Path) -> bool:
    """Stop checking that a token's issuer is the tenant's own.

    Plausible as a fix for "the integration tests do not have real tokens" — and it puts
    tenant selection back in the caller's hands.
    """
    return _replace(
        root / "src/attestor/policy/tenants.py",
        "        cls._check_provider(claims, tenant)",
        "        pass  # _check_provider(claims, tenant)",
    )


def _replay_a_stale_narrative(root: Path) -> bool:
    """Accept a recorded draft whose prompt has since changed.

    This is the narrative half of `StaleRecording`: without it, editing a prompt leaves the
    old prose in the report and every gate stays green, because the prose was valid — for a
    prompt nobody is using any more.
    """
    return _replace(
        root / "src/attestor/agent/narrative.py",
        'if entry.get("prompt_digest") != digest:',
        "if False:",
    )


def _let_paper_become_a_figure(root: Path) -> bool:
    """Admit extracted rows into a datapoint nothing reconciles them against.

    The plausible version of this mistake is not malice, it is a widening: somebody adds a
    row spec for a datapoint that has no `tolerance.cross_check`, the pipeline happily writes
    the rows, and a misread digit becomes a published figure with nothing in the path able to
    notice. Dropping the cross-check requirement is the same change, stated in one line.
    """
    return _replace(
        root / "src/attestor/datapoints/admissibility.py",
        "    if not tolerance.cross_check:",
        "    if False:",
    )


def _let_paper_back_the_primary(root: Path) -> bool:
    """Let extracted rows sit on the primary side of their own cross-check.

    Subtler and more likely than the above, because it still *looks* reconciled: the figure
    has a cross-check, the cross-check passes, and both sides came off the same scan. It
    proves the reader is consistent with itself.
    """
    return _replace(
        root / "src/attestor/datapoints/admissibility.py",
        "    if dataset in primary:",
        "    if False:",
    )


def _let_a_surface_choose_its_own_role(root: Path) -> bool:
    """Default the declared role instead of refusing without one.

    The plausible version of this mistake, which is why it is worth planting. Neither AgentCore
    surface forwards the caller's claims, so the handler cannot know the role — and "fall back
    to the least privilege" reads as caution while being a handler granting authority nobody
    wrote down and nobody reviewed. The safe state is no output, not the smallest output.
    """
    path = root / "src/attestor/agent/handler.py"
    text = path.read_text(encoding="utf-8")
    marker = 'role = os.environ.get(f"ATTESTOR_SURFACE_ROLE_{tenant_id.upper()}", "")'
    if marker not in text:
        return False
    path.write_text(
        text.replace(
            marker,
            'role = os.environ.get(f"ATTESTOR_SURFACE_ROLE_{tenant_id.upper()}", "role:reporter")',
        ),
        encoding="utf-8",
    )
    return True


def _let_a_caller_name_the_tenant_at_a_surface(root: Path) -> bool:
    """Trust `ATTESTOR_TENANT` from the event instead of from the resource.

    One runtime per tenant is what makes the tenant a fact about the resource. Reading it from
    the payload turns the strongest statement in the design into the weakest — a caller's word.
    """
    path = root / "src/attestor/agent/handler.py"
    text = path.read_text(encoding="utf-8")
    marker = '    tenant_id = os.environ.get("ATTESTOR_TENANT", "")'
    if marker not in text:
        return False
    path.write_text(
        text.replace(marker, '    tenant_id = os.environ.get("ATTESTOR_TENANT", "helios")'),
        encoding="utf-8",
    )
    return True


def _point_every_runtime_at_one_issuer(root: Path) -> bool:
    """Restore the shared runtime: one issuer, every tenant's client.

    This is not hypothetical. It is what was deployed until the audit found it — a
    `discovery_url` chosen by arbitrary map ordering with `allowed_clients` listing everybody,
    so one tenant could reach the runtime, the rest could not, and which one depended on how
    Terraform happened to iterate.
    """
    path = root / "infra/agent/main.tf"
    text = path.read_text(encoding="utf-8")
    marker = "allowed_clients = [aws_cognito_user_pool_client.tenant[each.value].id]"
    if marker not in text:
        return False
    path.write_text(
        text.replace(
            marker,
            "allowed_clients = [for client in aws_cognito_user_pool_client.tenant : client.id]",
        ),
        encoding="utf-8",
    )
    return True


def _drop_an_endpoint_the_runtime_needs(root: Path) -> bool:
    """Remove ECR from the VPC endpoints.

    The runtime keeps reporting READY and never starts, which is the most expensive shape a
    failure can take: everything says fine and nothing runs.
    """
    path = root / "infra/foundation/main.tf"
    text = path.read_text(encoding="utf-8")
    if '"ecr.dkr",' not in text:
        return False
    path.write_text(text.replace('    "ecr.dkr",\n', ""), encoding="utf-8")
    return True


def _let_the_surface_roles_drift(root: Path) -> bool:
    """Take an Athena permission off the runtime and leave the Lambda's alone.

    The failure arrives as `E_RESOLVER_ERROR`, which reads like a data problem and is a
    permission the other surface has.
    """
    path = root / "infra/agent/main.tf"
    text = path.read_text(encoding="utf-8")
    marker = '"s3:GetBucketLocation",'
    # Both roles grant it. Removing the last occurrence takes it from the runtime and leaves
    # the Lambda's, which is exactly the drift the gate exists to see.
    both_roles = 2
    if text.count(marker) < both_roles:
        return False
    last = text.rindex(marker)
    path.write_text(text[:last] + text[last + len(marker) :], encoding="utf-8")
    return True


def _let_one_pin_serve_every_table(root: Path) -> bool:
    """Choose the as-of pin by anything other than the table it is attached to.

    Claim 4's second half rests on a pin per table: a query and its cross-check read different
    ones, and a single pin for the pair is how a replay dies naming a snapshot id that exists —
    on the other table.
    """
    path = root / "src/attestor/datapoints/backends.py"
    text = path.read_text(encoding="utf-8")
    marker = 'pin = (snapshots or {}).get(last_table or "") or (snapshots or {}).get("*")'
    if marker not in text:
        return False
    path.write_text(
        text.replace(marker, "pin = next(iter((snapshots or {}).values()), None)"), encoding="utf-8"
    )
    return True


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "launder a resolver error into a lawful omission",
        "reason-code vocabulary",
        ["pytest", "-q", "tests/contracts/test_reason_codes.py", "-x"],
        "pipeline_failures_are_never_lawful",
        _launder_a_resolver_error,
        "One word. It turns every crash into 'not material' and the report still ships.",
    ),
    Mutation(
        "let a model write a number",
        "narrative rule",
        ["pytest", "-q", "tests/documents", "-x"],
        "narrative",
        _let_a_model_write_a_number,
        "Plausible as a fix for a citation-marker false positive.",
    ),
    Mutation(
        "delete the cross-tenant forbid",
        "Cedar isolation",
        ["attestor", "eval", "isolation"],
        "isolation",
        _drop_the_cross_tenant_forbid,
        "The policy everything else assumes is there.",
    ),
    Mutation(
        "drop the tenant from the cache key",
        "cache scoping",
        ["pytest", "-q", "tests/policy", "-x"],
        "tenant",
        _put_the_tenant_in_the_cache_key_last,
        "Looks like a harmless refactor; serves one tenant another's answer.",
    ),
    Mutation(
        "let a caller widen the retrieval filter",
        "retrieval scoping",
        ["pytest", "-q", "tests/retrieval", "-x"],
        "filter",
        _widen_the_retrieval_filter,
        "An 'extra filters' feature that quietly accepts a tenant override.",
    ),
    Mutation(
        "remove the imperative anchor from an injection rule",
        "injection false positives",
        ["attestor", "eval", "injection"],
        "benign",
        _soften_an_injection_rule,
        "Broadening a rule to catch more attacks, and catching real documents instead.",
    ),
    Mutation(
        "accept a dimension mismatch",
        "unit algebra",
        ["pytest", "-q", "tests/contracts/test_loader.py", "-x"],
        "dimension",
        _accept_a_dimension_mismatch,
        "The check that stops a figure being published a thousand times too small.",
    ),
    Mutation(
        "let a contract pre-authorize an internal failure",
        "abstention vocabulary",
        ["pytest", "-q", "tests/contracts/test_model.py", "-x"],
        "test_contract_cannot_pre_authorize_an_internal_failure",
        _let_a_contract_preauthorize_a_crash,
        "A contract declaring E_RESOLVER_ERROR as an acceptable omission.",
    ),
    Mutation(
        "let an agent approve an override",
        "authorization",
        ["pytest", "-q", "tests/policy", "-x"],
        "test_nobody_approves_an_override_through_the_agent",
        _let_an_agent_approve_an_override,
        "A second, weaker path to a signature.",
    ),
    Mutation(
        "render a report that has blockers",
        "issue refusal",
        ["pytest", "-q", "tests/documents", "-x"],
        "test_a_blocked_report_produces_no_artefact",
        _publish_despite_a_blocker,
        "The night-before-the-deadline change.",
    ),
    Mutation(
        "leave a forbid unterminated so the parser cannot see it",
        "Cedar parser",
        ["attestor", "policy", "verify"],
        "outside any policy",
        _make_a_policy_unparseable,
        "A one-character typo. The policy stays visible in the file and stops being enforced.",
    ),
    Mutation(
        "stop binding a token's issuer to its tenant",
        "issuer binding",
        ["attestor", "eval", "isolation"],
        "isolation",
        _drop_the_issuer_binding,
        "Removes the only thing making a caller-supplied tenant id safe.",
    ),
    Mutation(
        "let an extracted value reach a figure nothing reconciles it against",
        "extraction admissibility",
        ["pytest", "-q", "tests/datapoints/test_admissibility.py", "-x"],
        "no cross-check",
        _let_paper_become_a_figure,
        "A row spec added for a datapoint with no cross-check. The pipeline writes it, and "
        "a misread digit becomes a published figure with nothing able to notice.",
    ),
    Mutation(
        "let extracted rows back the primary side of their own cross-check",
        "extraction admissibility",
        ["pytest", "-q", "tests/datapoints/test_admissibility.py", "-x"],
        "cross-check side",
        _let_paper_back_the_primary,
        "Still looks reconciled — the figure has a cross-check and it passes. Both sides "
        "came off the same scan.",
    ),
    Mutation(
        "replay a narrative captured against an older prompt",
        "narrative staleness",
        ["pytest", "-q", "tests/agent/test_narrative.py", "-x"],
        "stale",
        _replay_a_stale_narrative,
        "Editing a prompt and shipping the previous prompt's prose, silently.",
    ),
    Mutation(
        "let a surface pick a role nobody declared",
        "declared surface role",
        ["pytest", "-q", "tests/agent/test_handler_and_gateway.py", "-x"],
        "no declared role",
        _let_a_surface_choose_its_own_role,
        "Reads as caution. Is a handler granting authority nobody wrote down.",
    ),
    Mutation(
        "let a runtime default its tenant",
        "surface tenancy",
        ["pytest", "-q", "tests/agent/test_handler_and_gateway.py", "-x"],
        "runtime",
        _let_a_caller_name_the_tenant_at_a_surface,
        "One runtime per tenant is the whole reason the tenant is a fact and not a claim.",
    ),
    Mutation(
        "let one runtime serve every tenant's clients",
        "per-tenant authorizer",
        [sys.executable, "scripts/check_agentcore_authorizers.py"],
        "one issuer",
        _point_every_runtime_at_one_issuer,
        "Exactly what was deployed until the audit found it.",
    ),
    Mutation(
        "take away an endpoint the runtime cannot start without",
        "VPC endpoints",
        [sys.executable, "scripts/check_vpc_endpoints.py"],
        "no endpoint",
        _drop_an_endpoint_the_runtime_needs,
        "Nothing refuses the connection. It waits, and the control plane says READY.",
    ),
    Mutation(
        "let the two surfaces' roles drift apart",
        "surface role parity",
        [sys.executable, "scripts/check_surface_roles_agree.py"],
        "missing",
        _let_the_surface_roles_drift,
        "Same handlers, separate roles. The failure arrives as an abstention.",
    ),
    Mutation(
        "let one as-of pin serve every table",
        "per-table pinning",
        ["pytest", "-q", "tests/datapoints/test_athena_backend.py", "-x"],
        "own_pin",
        _let_one_pin_serve_every_table,
        "A replay then reads one table at another table's instant, and mostly still answers.",
    ),
)


# ── Running them ─────────────────────────────────────────────────────────────


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # PYTHONPATH points at the *copy*, not at the editable install. Without this the mutation
    # is planted in a directory nothing imports from, every gate passes, and the script
    # reports a perfect score while proving nothing at all.
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(cwd / "src")
    return subprocess.run(  # noqa: S603 — fixed command list, no shell
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def main() -> int:
    print("gate-proof: establishing the baseline")
    baseline = _run([sys.executable, "-m", "pytest", "-q"], ROOT)
    if baseline.returncode != 0:
        print("the suite is not green; every mutation below would be meaningless", file=sys.stderr)
        print(baseline.stdout[-4000:], file=sys.stderr)
        return 1
    print("  baseline green\n")

    passes: list[str] = []
    failures: list[str] = []
    stale: list[str] = []

    for mutation in MUTATIONS:
        with tempfile.TemporaryDirectory() as raw:
            copy = Path(raw) / "attestor"
            shutil.copytree(
                ROOT,
                copy,
                ignore=shutil.ignore_patterns(
                    ".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "out"
                ),
            )
            try:
                applied = mutation.apply(copy)
            except (ValueError, OSError) as exc:
                applied = False
                print(f"         ({type(exc).__name__}: {exc})")
            if not applied:
                stale.append(mutation.name)
                print(f"  STALE  {mutation.name} — its target has moved; the proof is not running")
                continue

            result = _run(_argv(mutation.command), copy)
            output = (result.stdout + result.stderr).lower()

            if result.returncode == 0:
                failures.append(mutation.name)
                print(f"  FAIL   {mutation.name} — {mutation.gate} accepted the violation")
            elif mutation.expect.lower() not in output:
                failures.append(mutation.name)
                print(
                    f"  FAIL   {mutation.name} — something failed, but not {mutation.gate}; "
                    f"{mutation.expect!r} is absent from the output"
                )
            else:
                passes.append(mutation.name)
                print(f"  ok     {mutation.name} — refused by {mutation.gate}")

    print()
    print(f"gate-proof: {len(passes)} refused, {len(failures)} accepted, {len(stale)} stale")
    if stale:
        print("\nstale mutations point at code that has moved. Update them:", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
    return 1 if failures or stale else 0


def _argv(command: list[str]) -> list[str]:
    """The full argv for a mutation's check, run out of the mutated copy.

    `python -m` for the installed entry points, so the copy is the code under test rather than
    whatever is on PATH. A bare script path is run as a script — it used to fall through the
    `-m` branch and become `python -m /usr/bin/python scripts/foo.py`, which fails for a reason
    that has nothing to do with the gate. The harness then reported "something failed, but not
    <gate>", which is the correct thing for it to say and cost an hour to read as "the harness
    mangled your command" rather than "your gate is broken".

    That is the rule working: a non-zero exit is not evidence, the *named* check has to report
    the failure. It caught a malformed command exactly as it would catch a gate that fired for
    the wrong reason.
    """
    if command[0] == "pytest":
        return [sys.executable, "-m", "pytest", *command[1:]]
    if command[0] == "attestor":
        return [sys.executable, "-m", "attestor.cli.main", *command[1:]]
    return list(command)


if __name__ == "__main__":
    raise SystemExit(main())
