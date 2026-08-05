"""The front door. Every gate and every eval is reachable from here, and from CI.

One rule shapes the whole module: **a failing gate exits non-zero.** Not a warning, not a
summary line coloured amber. If `make claims` is green, every claim in the README held when
it ran; if it is red, none of them may be quoted. A command that prints a problem and exits
zero teaches people to ignore its output.

Nothing here touches AWS. The report commands render from recorded data, and the estate
commands are deliberately absent — deploying is a gated workflow, not a laptop verb.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import typer
from rich.console import Console

from attestor.contracts import loader, overrides
from attestor.contracts.model import Standard
from attestor.datapoints.backends import RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import NarrativeDraft, ResolutionContext, Resolver
from attestor.documents import render as render_module
from attestor.documents import writers
from attestor.documents.render import RenderContext, ReportBlocked
from attestor.documents.template import Template
from attestor.gates import abstention, provenance
from attestor.observability import dashboard as dashboard_module
from attestor.observability import run_record
from attestor.policy import cedar
from attestor.policy.tenants import TenantRegistry
from attestor.retrieval import bakeoff
from attestor.security import harness, isolation

app = typer.Typer(add_completion=False, help="Attestor — every number carries its proof.")
contracts_app = typer.Typer(help="The datapoint contract set.")
gate_app = typer.Typer(help="Acceptance gates.")
eval_app = typer.Typer(help="Scored, credential-free harnesses.")
report_app = typer.Typer(help="Render a tenant's report set.")
policy_app = typer.Typer(help="Cedar policies.")
retrieval_app = typer.Typer(help="Retrieval engineering.")
govern_app = typer.Typer(help="Generated governance documents.")

app.add_typer(contracts_app, name="contracts")
app.add_typer(gate_app, name="gate")
app.add_typer(eval_app, name="eval")
app.add_typer(report_app, name="report")
app.add_typer(policy_app, name="policy")
app.add_typer(retrieval_app, name="retrieval")
app.add_typer(govern_app, name="govern")

console = Console()
ROOT = Path.cwd()

PERIOD_START = dt.date(2026, 1, 1)
PERIOD_END = dt.date(2027, 1, 1)
REPORT_DATE = dt.date(2026, 7, 1)


def _fail(message: str) -> None:
    console.print(f"[bold red]FAIL[/] {message}")
    raise typer.Exit(code=1)


def _ok(message: str) -> None:
    console.print(f"[bold green]PASS[/] {message}")


def _narrative(_contract, _context) -> NarrativeDraft:
    """The offline narrative provider.

    It cites, it stays short, and it writes no digits — which is what the contract demands of
    a real model. Substituting it for Bedrock is how the whole document path stays testable
    without an account; substituting it for something that *would* fail the gates would make
    the local run a different program from the deployed one.
    """
    return NarrativeDraft(
        text=(
            "The undertaking maintains a board-approved transition plan covering its own "
            "operations and its upstream value chain. [ev:7f3a] Decarbonisation relies "
            "principally on fleet replacement and site electrification, sequenced against the "
            "capital plan. [ev:91c0] Board approval is evidenced in the minutes of the "
            "sustainability committee. [ev:2d55]"
        ),
        citations=("ev:7f3a", "ev:91c0", "ev:2d55"),
        prompt_ref="esrs_e1_1_transition_plan@3",
    )


def _contracts_for(tenant: str, root: Path):
    """Only the standard this tenant reports under.

    Handing a tenant the whole repository is how `lumen` ended up blocking on nine ESRS
    datapoints it was never required to disclose.
    """
    registry = TenantRegistry.load(root)
    return loader.load(root).for_standard(Standard(registry[tenant].standard))


def _resolver(tenant: str, root: Path) -> Resolver:
    return Resolver(
        contracts=_contracts_for(tenant, root),
        backend=RecordedBackend.from_directory(root / "recordings"),
        evidence=EvidenceIndex.for_tenant(root, tenant),
        override_register=overrides.load_register(root),
        root=root,
        narrative_provider=_narrative,
    )


def _resolve(tenant: str, root: Path, *, as_of: dt.date = REPORT_DATE):
    return _resolver(tenant, root).resolve_all(
        ResolutionContext(
            tenant=tenant,
            period="2026",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            as_of=as_of,
            run_id="cli",
        )
    )


# ── contracts ────────────────────────────────────────────────────────────────


@contracts_app.command("validate")
def contracts_validate(root: Path = typer.Option(ROOT, "--root")) -> None:
    """Schema, invariants and referential integrity across the whole contract set."""
    issues = loader.validate(root)
    if issues:
        for issue in issues:
            console.print(f"  {issue}")
        _fail(f"{len(issues)} contract problem(s)")
    contracts = loader.load(root)
    _ok(f"{len(contracts)} contract(s) consistent")


@contracts_app.command("list")
def contracts_list(root: Path = typer.Option(ROOT, "--root")) -> None:
    for contract in sorted(loader.load(root), key=lambda c: c.id):
        marker = "narrative" if contract.is_model_authored else (contract.unit or "—")
        console.print(f"  {contract.id:<40} {marker:<14} {contract.reference}")


# ── gates ────────────────────────────────────────────────────────────────────


@gate_app.command("provenance")
def gate_provenance(
    root: Path = typer.Option(ROOT, "--root"),
    tenant: str = typer.Option("helios", "--tenant"),
    out: Path = typer.Option(Path("out"), "--out"),
    all_tenants: bool = typer.Option(False, "--all", help="Every tenant that can issue."),
) -> None:
    """CLAIM 3 — render, then inspect the finished files for numerals nobody can account for."""
    tenants = [t.id for t in TenantRegistry.load(root)] if all_tenants else [tenant]
    pairs: list[tuple[Path, object]] = []
    skipped: list[str] = []

    for name in tenants:
        try:
            documents = _render(name, root, out)
        except ReportBlocked as blocked:
            skipped.append(f"{name}: {len(blocked.blockers)} blocker(s)")
            continue
        pairs.extend(documents)

    for note in skipped:
        console.print(f"  [yellow]skipped[/] {note} — a blocked report produces no artefact")

    if not pairs:
        _fail("no artefact was produced, so nothing was checked")

    results = provenance.check_all(pairs)
    console.print(provenance.report(results))
    if any(not result.passed for result in results):
        _fail("provenance gate")
    _ok(f"{len(results)} artefact(s) clean")


def _render(tenant: str, root: Path, out: Path):
    results = _resolve(tenant, root)
    registry = TenantRegistry.load(root)
    context = RenderContext(
        tenant_id=tenant,
        tenant_name=registry[tenant].name,
        period="2026",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        report_date=REPORT_DATE,
    )
    contracts = _contracts_for(tenant, root)
    produced = []
    for template in Template.load_all(root / "templates"):
        # A tenant's templates are the ones for its standard. Rendering an ESRS statement for
        # an AI Act engagement would fail on the first placeholder, and failing later is
        # always worse than not starting.
        if template.standard != registry[tenant].standard:
            continue
        document = render_module.render(
            template, results=results, contracts=contracts, context=context
        )
        path = out / tenant / f"{template.id}.{template.artefact}"
        writers.write(document, path)
        writers.write_manifest(document, path.with_suffix(f".{template.artefact}.manifest.json"))
        produced.append((path, document.manifest))
    return produced


# ── evals ────────────────────────────────────────────────────────────────────


@eval_app.command("abstention")
def eval_abstention(root: Path = typer.Option(ROOT, "--root")) -> None:
    """CLAIM 5 — exactly N refusals, zero fabrications, nothing else refused."""
    score = abstention.run(root)
    console.print(score.report())
    if not score.passed:
        _fail("abstention")
    _ok(score.summary())


@eval_app.command("injection")
def eval_injection(root: Path = typer.Option(ROOT, "--root")) -> None:
    """CLAIM 1 — poisoned documents flagged, benign documents untouched."""
    score = harness.run(root / "evals" / "injection" / "corpus.yaml")
    console.print(score.report())
    if not score.passed:
        _fail("injection")
    _ok(score.summary())


@eval_app.command("isolation")
def eval_isolation(root: Path = typer.Option(ROOT, "--root")) -> None:
    """CLAIM 2 — twelve routes between tenants, all closed."""
    report = isolation.run(root)
    console.print(report.report())
    if not report.passed:
        _fail("isolation")
    _ok(report.summary())


@eval_app.command("reproducibility")
def eval_reproducibility(
    root: Path = typer.Option(ROOT, "--root"),
    tenant: str = typer.Option("helios", "--tenant"),
) -> None:
    """CLAIM 4 — the same data resolves to the same values and the same lineage."""
    from attestor.datapoints.resolver import summarise  # noqa: PLC0415 — local to the command

    first = summarise(_resolve(tenant, root))
    second = summarise(_resolve(tenant, root))
    if first != second:
        differing = sorted(
            key
            for key in first["lineage"]
            if first["lineage"].get(key) != second["lineage"].get(key)
        )
        _fail(f"resolution is not reproducible; lineage differs for {', '.join(differing) or '?'}")
    _ok(f"{len(first['lineage'])} lineage id(s) identical across two runs")


@eval_app.command("retrieval")
def eval_retrieval(
    root: Path = typer.Option(ROOT, "--root"), k: int = typer.Option(3, "--k")
) -> None:
    """Score the golden retrieval set."""
    result = bakeoff.run(root, k=k)
    console.print(result.table())
    for score in result.scores:
        if score.unanswerable:
            _fail(f"{score.variant.id} splits {score.unanswerable} answer(s) across chunks")
    _ok("no chunking strategy splits an answer")


# ── policy ───────────────────────────────────────────────────────────────────


@policy_app.command("verify")
def policy_verify(root: Path = typer.Option(ROOT, "--root")) -> None:
    """Parse every policy, then run the attack set. Every attack must be denied."""
    policies = cedar.load(root)
    console.print(f"  {len(policies)} policy(ies): {', '.join(policies.ids)}")
    unnamed = [pid for pid in policies.ids if "#" in pid]
    if unnamed:
        _fail(f"policy(ies) without an @id: {', '.join(unnamed)}")
    report = isolation.run(root)
    if not report.passed:
        console.print(report.report())
        _fail("policy attack set")
    _ok("policies parse, and every attack is denied")


# ── report ───────────────────────────────────────────────────────────────────


@report_app.command("render")
def report_render(
    tenant: str = typer.Option(..., "--tenant"),
    root: Path = typer.Option(ROOT, "--root"),
    out: Path = typer.Option(Path("out"), "--out"),
) -> None:
    """Render a tenant's DOCX, XLSX and PPTX — or refuse, loudly."""
    try:
        produced = _render(tenant, root, out)
    except ReportBlocked as blocked:
        console.print(str(blocked))
        _fail(f"{tenant}: report blocked, no artefact written")
    for path, _ in produced:
        console.print(f"  wrote {path}")
    _ok(f"{len(produced)} artefact(s) for {tenant}")


@report_app.command("status")
def report_status(
    tenant: str = typer.Option(..., "--tenant"), root: Path = typer.Option(ROOT, "--root")
) -> None:
    """What would be published, what would be omitted, and what blocks."""
    results = _resolve(tenant, root)
    console.print(f"  published:   {len(results.published)}")
    console.print(f"  limitations: {len(results.limitations)}")
    for limitation in results.limitations:
        console.print(f"      {limitation.datapoint_id}: {limitation.reason_code}")
    console.print(f"  blockers:    {len(results.blockers)}")
    for blocker in results.blockers:
        console.print(
            f"      [red]{blocker.datapoint_id}[/]: {blocker.reason_code} — {blocker.detail}"
        )
    if not results.can_issue:
        _fail(f"{tenant} cannot issue")
    _ok(f"{tenant} can issue")


# ── retrieval ────────────────────────────────────────────────────────────────


@retrieval_app.command("bake-off")
def retrieval_bakeoff(
    root: Path = typer.Option(ROOT, "--root"),
    k: int = typer.Option(3, "--k"),
    replay: bool = typer.Option(True, "--replay/--live"),
) -> None:
    """Compare chunking strategies. `--live` is not implemented until the estate exists."""
    if not replay:
        _fail("a live bake-off needs the estate; `--replay` is the only supported mode today")
    console.print(bakeoff.run(root, k=k).table())


# ── govern ───────────────────────────────────────────────────────────────────


@govern_app.command("generate")
def govern_generate(
    root: Path = typer.Option(ROOT, "--root"),
    check: bool = typer.Option(False, "--check", help="Fail if the committed docs are stale."),
) -> None:
    """Regenerate the governance documents from the code that enforces them."""
    from attestor.observability.governance import generate  # noqa: PLC0415

    stale = generate(root, check=check)
    if stale:
        for name in stale:
            console.print(f"  stale: {name}")
        _fail("governance docs are out of date; run `make govern-docs`")
    _ok("governance docs in sync")


# ── run ──────────────────────────────────────────────────────────────────────


@app.command("run")
def run_report(
    tenant: str = typer.Option(..., "--tenant"),
    root: Path = typer.Option(ROOT, "--root"),
    out: Path = typer.Option(Path("out"), "--out"),
    run_id: str = typer.Option("local", "--run-id"),
) -> None:
    """Resolve, render, gate, and record — the whole pipeline for one tenant.

    A blocked run still writes its record. That is the point: "we could not issue, and here
    is exactly what stopped us" is the most useful output this system produces, and throwing
    it away because no document was written would leave the failure undocumented.
    """
    started = dt.datetime.now(dt.UTC)
    registry = TenantRegistry.load(root)
    tenant_config = registry[tenant]
    contracts = _contracts_for(tenant, root)
    results = _resolve(tenant, root)

    record = run_record.build(
        run_id=run_id,
        tenant=tenant,
        tenant_name=tenant_config.name,
        standard=tenant_config.standard,
        period="2026",
        started_at=started,
        results=results,
        contracts=contracts,
    )

    if results.can_issue:
        produced = _render(tenant, root, out)
        gate_results = provenance.check_all(produced)
        for (path, _), gate in zip(produced, gate_results, strict=True):
            record.artefacts.append(
                run_record.ArtefactRecord(
                    path=str(path.relative_to(out)),
                    artefact=path.suffix.lstrip("."),
                    sha256=run_record.digest_file(path),
                    numerals_checked=gate.numerals_checked,
                    provenance_clean=gate.passed,
                )
            )
            record.gates.append(
                run_record.GateRecord(
                    name=f"provenance:{path.name}",
                    passed=gate.passed,
                    detail=gate.summary(),
                )
            )
        console.print(f"  {len(produced)} artefact(s) under {out / tenant}")
    else:
        console.print(f"  [red]blocked[/] — {len(results.blockers)} datapoint(s), no artefact")
        for blocker in results.blockers:
            console.print(f"      {blocker.datapoint_id}: {blocker.reason_code} — {blocker.detail}")

    record.finished_at = dt.datetime.now(dt.UTC)
    path = record.write(out / "runs")
    console.print(f"  recorded {path}")

    if not record.issued:
        _fail(f"{tenant} could not issue; the record says why")
    failed_gates = [gate for gate in record.gates if not gate.passed]
    if failed_gates:
        _fail(f"{len(failed_gates)} gate(s) failed")
    _ok(
        f"{tenant} issued · {len(record.published)} disclosed · "
        f"{len(record.limitations)} limitation(s)"
    )


@app.command("dashboard")
def build_dashboard(
    out: Path = typer.Option(Path("out"), "--out"),
    target: Path = typer.Option(Path("out/dashboard.html"), "--target"),
) -> None:
    """Build the static page from every recorded run."""
    records = run_record.RunRecord.load_all(out / "runs")
    if not records:
        _fail(f"no run records under {out / 'runs'}; run `attestor run --tenant <id>` first")
    path = dashboard_module.write(records, target)
    issued = sum(1 for record in records if record.issued)
    _ok(f"{path} — {len(records)} run(s), {issued} issued")


# ── cost ─────────────────────────────────────────────────────────────────────


@app.command("cost")
def cost_status() -> None:
    """What the estate is costing. Requires a live estate; absent by design offline."""
    console.print(
        "No estate is standing. Cost telemetry is collected per session while one runs — "
        "see src/attestor/observability/cost.py."
    )


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
