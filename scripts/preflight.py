#!/usr/bin/env python3
"""Everything that must be true before the estate is stood up, in one command.

The point of collecting these is not convenience. It is that "is it ready?" should have one
answer produced the same way every time, rather than a person remembering nine commands and
forgetting the tenth — which will be the one that mattered.

Three groups, and they fail differently:

**Correctness** — the suite, the gates, the evals. These are the claims the README makes. A
failure here means a claim is false right now.

**Consistency** — recordings against queries, seed against recordings, governance docs
against code, evidence classes against contracts. Nothing is broken; two things that must
agree have stopped agreeing, and the drift is invisible until something reads both.

**Deployability** — Terraform validates against real provider schemas, checkov is clean, the
Lambda payload builds, the container's build context is complete. These do not affect an
offline run at all, and each one is a deploy that fails at minute forty.

`--fast` skips the slow members of each group; CI runs the whole thing.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tool(name: str, fallback: str | None = None) -> str:
    """The venv's copy when there is a venv, whatever is on PATH when there is not.

    Hard-coding `.venv/bin/…` makes preflight a check that only runs where it was written.
    That is exactly how `make package` reached a deploy untested: it was green on every
    laptop and had never once executed on a runner.
    """
    candidate = ROOT / ".venv" / "bin" / name
    return str(candidate) if candidate.exists() else (fallback or name)


PYTHON = _tool("python", sys.executable)
RUFF = _tool("ruff")
#: checkov lives in its own environment, because it pins `boto3==1.35.49` exactly and the
#: application needs `bedrock-agentcore`, which botocore only learned about later. `make
#: iac-scan` creates `.venv-checkov` on demand; this finds it, and falls back to whatever is on
#: PATH so a runner that installed it another way still runs the scan.
CHECKOV = str(_CV) if (_CV := ROOT / ".venv-checkov" / "bin" / "checkov").exists() else "checkov"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Check:
    group: str
    name: str
    command: list[str]
    #: Why a reader should care that this passed. Printed on failure, because a red line
    #: without a reason is a red line somebody reruns hoping it goes away.
    matters: str
    slow: bool = False
    #: Skipped, with a note, when the tool is absent rather than failing the run. A preflight
    #: that cannot run without Terraform installed is a preflight nobody runs.
    needs: str | None = None


CHECKS: list[Check] = [
    # ── Correctness ─────────────────────────────────────────────────────────
    Check(
        "correctness",
        "test suite",
        [PYTHON, "-m", "pytest", "-q"],
        "Every claim in the README is asserted by one of these.",
    ),
    Check(
        "correctness",
        "claim 1 · injection",
        [PYTHON, "-m", "attestor.cli.main", "eval", "injection"],
        "Poisoned documents flagged, and — the number that matters — no benign one is.",
    ),
    Check(
        "correctness",
        "claim 2 · isolation",
        [PYTHON, "-m", "attestor.cli.main", "eval", "isolation"],
        "Twelve distinct routes between tenants, all closed.",
    ),
    Check(
        "correctness",
        "claim 3 · provenance",
        [PYTHON, "-m", "attestor.cli.main", "gate", "provenance", "--all"],
        "No numeral reaches a rendered document without a datapoint behind it.",
    ),
    Check(
        "correctness",
        "claim 4 · reproducibility",
        [PYTHON, "-m", "attestor.cli.main", "eval", "reproducibility"],
        "The same data resolves to the same figures and the same lineage.",
    ),
    Check(
        "correctness",
        "claim 5 · abstention",
        [PYTHON, "-m", "attestor.cli.main", "eval", "abstention"],
        "Exactly the expected refusals, zero fabrications, nothing else refused.",
    ),
    Check(
        "correctness",
        "authorization",
        [PYTHON, "-m", "attestor.cli.main", "policy", "verify"],
        "Cedar parses and every attack in the suite is denied.",
    ),
    Check(
        "correctness",
        "retrieval golden set",
        [PYTHON, "-m", "attestor.cli.main", "eval", "retrieval"],
        "No chunking strategy splits an answer across two chunks.",
    ),
    Check(
        "correctness",
        "gate-proof",
        [PYTHON, "scripts/gate_proof.py"],
        "Each gate refuses a real violation, for the right reason. Slow, and the most "
        "informative thing here: a gate that has never been shown to fail is a comment.",
        slow=True,
    ),
    # ── Consistency ─────────────────────────────────────────────────────────
    Check(
        "consistency",
        "contracts",
        [PYTHON, "-m", "attestor.cli.main", "contracts", "validate"],
        "Referential integrity: every query, prompt and operand a contract names exists.",
    ),
    Check(
        "consistency",
        "recordings match queries",
        [PYTHON, "scripts/seed_recordings.py", "--check"],
        "A query edited without re-capture would replay the previous version's answer.",
    ),
    Check(
        "consistency",
        "seed reproduces recordings",
        [PYTHON, "pipelines/seed/generate.py", "--check"],
        "If the seeded lake disagrees with the recordings, offline and live are different "
        "programs and every claim above is only approximately true.",
    ),
    Check(
        "consistency",
        "governance docs",
        [PYTHON, "-m", "attestor.cli.main", "govern", "generate", "--check"],
        "The generated control descriptions still match the code that enforces them.",
    ),
    Check(
        "consistency",
        "regulatory corpus",
        [PYTHON, "pipelines/ingest/regulatory.py", "--check"],
        "A datapoint with no corpus entry is one the model can never find guidance for, and "
        "the failure is invisible: retrieval returns something, just not this.",
    ),
    Check(
        "consistency",
        "guardrail alignment",
        [PYTHON, "scripts/check_guardrail_alignment.py"],
        "A contract stating a grounding threshold the deployed guardrail does not enforce is "
        "a disclosure about a control that does not exist.",
    ),
    Check(
        "consistency",
        "evidence manifests",
        [PYTHON, "pipelines/ingest/evidence.py"],
        "Every document is filed under a class some contract actually requires.",
    ),
    Check(
        "consistency",
        "knowledge base metadata",
        [sys.executable, "scripts/generate_kb_metadata.py", "--check"],
        "Retrieval is filtered at the index, and Bedrock reads the attributes from a sidecar "
        "beside each document. Without them every filtered query matches nothing, and the "
        "ingestion job reports it only as `numberOfMetadataDocumentsScanned: 0`.",
    ),
    Check(
        "consistency",
        "override register",
        [PYTHON, "scripts/check_overrides.py", "--warn-days", "30"],
        "No accepted defect has outlived its acceptance.",
    ),
    Check(
        "consistency",
        "lint",
        [RUFF, "check", "src", "tests", "scripts", "pipelines"],
        "The same command CI runs.",
    ),
    Check(
        "consistency",
        "format",
        [RUFF, "format", "--check", "src", "tests", "scripts", "pipelines"],
        "The same command CI runs.",
    ),
    # ── Deployability ───────────────────────────────────────────────────────
    Check(
        "deployability",
        "terraform fmt",
        ["terraform", "fmt", "-check", "-recursive", "infra"],
        "Formatting drift makes a real diff unreadable.",
        needs="terraform",
    ),
    Check(
        "deployability",
        "terraform validate",
        [sys.executable, "scripts/tf_validate.py"],
        "Against real provider schemas. This is what catches an attribute that does not "
        "exist, and it is the difference between 'it should apply' and 'it applies'.",
        slow=True,
        needs="terraform",
    ),
    Check(
        "deployability",
        "checkov",
        [CHECKOV, "-d", "infra", "--compact", "--quiet"],
        "Zero findings, with every deliberate exception carrying a written reason.",
        slow=True,
        needs=CHECKOV,
    ),
    Check(
        "deployability",
        "lambda payload",
        ["make", "package"],
        "terraform's archive_file has something to zip. Without it the apply fails on a "
        "missing file, after the expensive layers are already up.",
        slow=True,
    ),
    Check(
        "deployability",
        "MCP tool schema",
        [PYTHON, "-m", "attestor.cli.main", "gateway", "spec", "--check"],
        "Terraform configures the gateway target from this file. A tool added to SPECS "
        "without regenerating it is a handler the Gateway never exposes.",
    ),
    Check(
        "deployability",
        "gateway target",
        [sys.executable, "scripts/check_gateway_target.py"],
        "The tools are attached by a provisioner because no provider has the resource. This "
        "turns red the day one ships, so the workaround is removed by a build and not by "
        "somebody remembering.",
        slow=True,
        needs="terraform",
    ),
    Check(
        "deployability",
        "dbt project parses",
        [PYTHON, "-m", "pytest", "-q", "tests/pipelines/test_dbt_project.py"],
        "A dangling `ref` or an undeclared package fails `dbt build` at parse time — after "
        "the infrastructure is already up, which is the expensive place to find out.",
    ),
    Check(
        "deployability",
        "container build context",
        [sys.executable, "scripts/check_docker_context.py"],
        "Every path the Dockerfile copies exists. A COPY of a missing directory fails the "
        "build after the image layers are pushed.",
    ),
    Check(
        "deployability",
        "lakehouse wiring",
        [sys.executable, "scripts/check_lakehouse_wiring.py"],
        "Terraform, dbt and the queries describe one lakehouse three times. When they drift, "
        "`dbt parse` still passes and the first live build fails on a table that never "
        "existed — or worse, a resolver reads an empty table and calls it zero.",
    ),
    Check(
        "deployability",
        "AgentCore policies",
        [sys.executable, "scripts/check_agentcore_policies.py"],
        "The deployed policy set still holds the rule it exists for — no override through the "
        "agent — and asserts nothing about token claims this repository has not verified.",
    ),
    Check(
        "correctness",
        "AgentCore wiring",
        [sys.executable, "scripts/check_agentcore_wiring.py"],
        "Terraform and the agent code agree on every name they pass between them. "
        "`tenants/*.yaml` carried `eu-central-1_EXAMPLE` as the Cognito issuer, nothing "
        "substituted it, and the handler compares a token's `iss` against it — so the whole "
        "AgentCore path would have refused the first real token, on every deploy that has run.",
    ),
    Check(
        "correctness",
        "AgentCore authorizers",
        [sys.executable, "scripts/check_agentcore_authorizers.py"],
        "Every surface authenticates against exactly one tenant. The shared runtime chose its "
        "pool by map ordering and admitted every tenant's clients — not a leak, because the "
        "session binding held, but one tenant served and the rest locked out by iteration order.",
    ),
    Check(
        "correctness",
        "VPC endpoints",
        [sys.executable, "scripts/check_vpc_endpoints.py"],
        "Everything reached from inside the VPC has a way out of it. Egress is the VPC and "
        "nothing else, so a service with no endpoint does not fail — it hangs. Memory did, for "
        "180 seconds; the runtime could never pull its own image and reported READY throughout.",
    ),
    Check(
        "correctness",
        "surface roles agree",
        [sys.executable, "scripts/check_surface_roles_agree.py"],
        "The Lambda and the runtime run identical handlers under separate roles, and separate "
        "roles drift. Both failed Athena on the same missing `s3:GetBucketLocation`, months "
        "apart, because only one of them had ever been called.",
    ),
    Check(
        "deployability",
        "AgentCore names",
        [sys.executable, "scripts/check_agentcore_names.py"],
        "AgentCore rejects a name with a hyphen where it wants underscores, and "
        "`terraform validate` cannot see it — those validators run in the plan phase. This "
        "is the offline stand-in for a plan nobody can run without credentials.",
    ),
    Check(
        "deployability",
        "OIDC subjects",
        [sys.executable, "scripts/check_oidc_subjects.py"],
        "Every subject the deploy role trusts names this repository and one environment, "
        "with no wildcard. CKV_AWS_358 cannot parse GitHub's immutable subject and reads "
        "only the first value, so this is the check that actually covers the trust.",
    ),
]


@dataclass
class Result:
    check: Check
    status: str
    seconds: float
    output: str = ""


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if r.status == "fail"]

    @property
    def skipped(self) -> list[Result]:
        return [r for r in self.results if r.status == "skip"]

    @property
    def ok(self) -> bool:
        return not self.failed


def run(check: Check) -> Result:
    if check.needs and not (shutil.which(check.needs) or Path(check.needs).exists()):
        return Result(check, "skip", 0.0, f"{check.needs} is not installed")
    started = time.monotonic()
    completed = subprocess.run(  # noqa: S603 — fixed command lists, no shell
        check.command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
    )
    elapsed = time.monotonic() - started
    status = "pass" if completed.returncode == 0 else "fail"
    return Result(check, status, elapsed, (completed.stdout + completed.stderr)[-3000:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="Skip the slow checks.")
    parser.add_argument("--group", help="Run one group only.")
    args = parser.parse_args()

    selected = [
        check
        for check in CHECKS
        if not (args.fast and check.slow) and (not args.group or check.group == args.group)
    ]

    report = Report()
    current_group = ""
    for check in selected:
        if check.group != current_group:
            current_group = check.group
            print(f"\n{DIM}── {current_group}{RESET}")
        print(f"   {check.name:<32}", end="", flush=True)
        result = run(check)
        report.results.append(result)
        mark = {
            "pass": f"{GREEN}ok{RESET}",
            "fail": f"{RED}FAIL{RESET}",
            "skip": f"{YELLOW}skip{RESET}",
        }
        print(f"{mark[result.status]}  {DIM}{result.seconds:5.1f}s{RESET}")

    print()
    for result in report.skipped:
        print(f"{YELLOW}skipped{RESET} {result.check.name}: {result.output}")

    for result in report.failed:
        print(f"\n{RED}FAILED{RESET} {result.check.name}")
        print(f"  why it matters: {result.check.matters}")
        print(f"{DIM}{result.output.rstrip()}{RESET}")

    passed = sum(1 for r in report.results if r.status == "pass")
    print(
        f"\npreflight: {passed} passed, {len(report.failed)} failed, {len(report.skipped)} skipped"
    )
    if report.ok:
        print("the repository is ready to deploy; nothing here has been deployed")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
