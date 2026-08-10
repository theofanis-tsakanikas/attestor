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
import os
import sys
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console

from attestor.agent import narrative
from attestor.contracts import loader, overrides
from attestor.contracts.model import Standard
from attestor.datapoints.backends import AthenaBackend, QueryBackend, RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import ResolutionContext, Resolver
from attestor.documents import render as render_module
from attestor.documents import writers
from attestor.documents.manifest import NumeralInNarrative
from attestor.documents.render import RenderContext, ReportBlocked
from attestor.documents.template import Template, TemplateError
from attestor.gates import abstention, provenance
from attestor.observability import dashboard as dashboard_module
from attestor.observability import run_record
from attestor.observability.cost import CostMeter
from attestor.policy import cedar
from attestor.policy.tenants import Session, TenantRegistry
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
gateway_app = typer.Typer(help="The MCP surface AgentCore Gateway exposes.")
evidence_app = typer.Typer(help="Evidence a corpus holds, generated from the runs it describes.")

app.add_typer(contracts_app, name="contracts")
app.add_typer(gate_app, name="gate")
app.add_typer(eval_app, name="eval")
app.add_typer(report_app, name="report")
app.add_typer(policy_app, name="policy")
app.add_typer(retrieval_app, name="retrieval")
app.add_typer(govern_app, name="govern")
app.add_typer(gateway_app, name="gateway")
app.add_typer(evidence_app, name="evidence")

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


def _backend(root: Path) -> QueryBackend:
    """Recorded by default; Athena when the estate is up.

    The default is not a convenience. Every gate and eval in this repository replays
    recordings, and a command that silently reached for a live account would make an offline
    run depend on credentials nobody has.

    But the inverse was a real defect: the deploy workflow's "run against the live estate"
    step used to replay the recordings too, so it printed PASS without touching Athena. A
    deploy that appears to succeed while proving nothing is worse than one that fails —
    `ATTESTOR_BACKEND=athena` is what the workflow now sets, and the environment it needs is
    asserted here rather than defaulted.
    """
    if os.environ.get("ATTESTOR_BACKEND", "recorded").lower() != "athena":
        return RecordedBackend.from_directory(root / "recordings")

    missing = [
        name
        for name in ("ATTESTOR_WORKGROUP", "ATTESTOR_DATABASE", "ATTESTOR_ATHENA_OUTPUT")
        if not os.environ.get(name)
    ]
    if missing:
        _fail(
            "ATTESTOR_BACKEND=athena but " + ", ".join(missing) + " are unset. Falling back to "
            "recordings here would produce a green run that queried nothing."
        )
    return AthenaBackend(
        workgroup=os.environ["ATTESTOR_WORKGROUP"],
        catalog=os.environ.get("ATTESTOR_CATALOG", "AwsDataCatalog"),
        database=os.environ["ATTESTOR_DATABASE"],
        output_location=os.environ["ATTESTOR_ATHENA_OUTPUT"],
        region=os.environ.get("AWS_REGION", "eu-central-1"),
    )


def _contracts_for(tenant: str, root: Path):
    """Only the standard this tenant reports under.

    Handing a tenant the whole repository is how `lumen` ended up blocking on nine ESRS
    datapoints it was never required to disclose.
    """
    registry = TenantRegistry.load(root)
    return loader.load(root).for_standard(Standard(registry[tenant].standard))


def _narrative_provider(tenant: str, root: Path):
    """Recorded offline, Bedrock against a live estate — the same switch as `_backend`.

    An earlier version passed a paragraph written by hand in this file, and `attestor run`
    rendered it into a real DOCX under a tenant's name. Prose now comes from a reviewed
    recording or from a model, and from nowhere else; a missing recording is a refusal.
    """
    session = None
    if os.environ.get("ATTESTOR_BACKEND", "recorded").lower() == "athena":
        session = Session(
            tenant=tenant,
            subject="cli",
            roles=frozenset({"role:preparer"}),
            period="2026",
            session_id=f"cli-{tenant}",
        )
    return narrative.build(root, session=session)


def _resolver(tenant: str, root: Path, *, cost_meter: CostMeter | None = None) -> Resolver:
    """The resolver for one tenant, metered.

    `cost_meter` was optional and nobody ever passed one. `Resolver._meter` returns immediately
    when it is `None`, so every Athena scan and every model token was priced, attributed to a
    tenant and an operation, and thrown away — and every live run wrote `cost_eur = 0.0000`
    after querying a real lakehouse and drafting against a real model.

    `€/report` and `€/tenant` are named in the project's own description as a first-class
    metric rather than an afterthought. A first-class metric that is always zero is an
    afterthought with a nicer sentence in front of it.
    """
    return Resolver(
        contracts=_contracts_for(tenant, root),
        backend=_backend(root),
        evidence=EvidenceIndex.for_tenant(root, tenant),
        override_register=overrides.load_register(root),
        root=root,
        narrative_provider=_narrative_provider(tenant, root),
        cost_meter=cost_meter,
    )


def snapshots_from(record: run_record.RunRecord) -> dict[str, str]:
    """The snapshot each table was read at, keyed by table.

    A run record already carries this: every published figure lists its sources as
    `gold.ghg_scope_1_activity@7705096761963662595`. That is the receipt. Feeding it back in is
    what turns the receipt into a replay — the difference between being able to say which
    version of the data produced a number and being able to produce the number again.
    """
    pins: dict[str, str] = {}
    for published in record.published:
        for source in published.sources:
            table, _, snapshot = source.partition("@")
            if snapshot:
                pins[table] = snapshot
    return pins


def _resolve(
    tenant: str,
    root: Path,
    *,
    as_of: dt.date = REPORT_DATE,
    cost_meter: CostMeter | None = None,
    snapshots: dict[str, str] | None = None,
):
    return _resolver(tenant, root, cost_meter=cost_meter).resolve_all(
        ResolutionContext(
            tenant=tenant,
            period="2026",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            as_of=as_of,
            run_id="cli",
            snapshots=snapshots or {},
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


def _render(tenant: str, root: Path, out: Path, results=None):
    """Render a tenant's documents from results that have already been judged.

    `results` is a parameter because this function used to call `_resolve` itself, and a
    caller that had *just* resolved in order to decide whether a report could be issued then
    resolved again to produce it. Two resolutions, two sets of model calls, two sets of Athena
    queries — and, because a narrative draft is not deterministic, two different answers. A run
    was observed where the first resolution issued and the second raised `ReportBlocked`.

    The cost was the smaller half. The document was being built from results other than the
    ones the run record described, so the manifest, the lineage and the artefact could all
    disagree about the same figure and every one of them would look internally consistent.
    """
    if results is None:
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


# ── gateway ──────────────────────────────────────────────────────────────────


@gateway_app.command("spec")
def gateway_spec(
    root: Path = typer.Option(ROOT, "--root"),
    check: bool = typer.Option(False, "--check", help="Fail if the committed schema is stale."),
) -> None:
    """Write the MCP tool schema the Gateway target is configured with.

    Terraform reads the committed file rather than calling Python at plan time, so the file
    is an artefact and can drift. `--check` is what stops it: a tool added to `SPECS` without
    regenerating this would be a handler the Gateway never exposes.
    """
    from attestor.agent import gateway  # noqa: PLC0415 — keeps the import surface honest

    target = root / "infra" / "agent" / "tools.openapi.json"
    rendered = gateway.render_tool_schema()
    if check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != rendered:
            _fail(f"{target.relative_to(root)} is stale; run `make gateway-spec`")
        _ok(f"{len(gateway.tool_schema()['tools'])} tool(s) described, schema in sync")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    _ok(f"wrote {target.relative_to(root)}")


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


# ── evidence ─────────────────────────────────────────────────────────────────


@evidence_app.command("export")
def evidence_export(
    root: Path = typer.Option(ROOT, "--root"),
    check: bool = typer.Option(False, "--check", help="Fail if the committed evidence is stale."),
) -> None:
    """Regenerate `lumen`'s measured evidence, and reconcile every declared digest."""
    from attestor.evals import export  # noqa: PLC0415

    stale = export.generate(root, check=check)
    drifted = export.reconcile(root, check=check)
    for name in [*stale, *drifted]:
        console.print(f"  stale: {name}")
    if stale or drifted:
        _fail("evidence is out of date; run `make evidence`")
    if unchecked := export.unverifiable(root):
        # Not a failure. These are dumps of lake tables and their bytes are genuinely not
        # here — but a corpus that reported six verified documents and stayed silent about
        # the other two would be claiming more than it checked.
        console.print(f"  digest unverifiable offline: {', '.join(unchecked)}")
    _ok("evidence matches the runs it describes")


# ── run ──────────────────────────────────────────────────────────────────────


@app.command("run")
def run_report(
    tenant: str = typer.Option(..., "--tenant"),
    root: Path = typer.Option(ROOT, "--root"),
    out: Path = typer.Option(Path("out"), "--out"),
    run_id: str = typer.Option("local", "--run-id"),
    replay: Path | None = typer.Option(None, "--replay", help="a prior run record to re-resolve"),
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
    meter = CostMeter()
    # `--replay` is claim 4's second half. The first — the same data resolving to the same
    # values — has always held. This is re-resolving *as of an earlier instant*: the pins come
    # from the prior record's own lineage, so the query reads the table as it stood when the
    # figure was published rather than as it stands now.
    pins = snapshots_from(run_record.RunRecord.load(replay)) if replay else None
    if pins:
        console.print(f"replaying {replay} — {len(pins)} datapoint(s) pinned to their snapshots")
    results = _resolve(tenant, root, cost_meter=meter, snapshots=pins)

    record = run_record.build(
        run_id=run_id,
        tenant=tenant,
        tenant_name=tenant_config.name,
        standard=tenant_config.standard,
        period="2026",
        started_at=started,
        results=results,
        contracts=contracts,
        cost_meter=meter,
    )

    if results.can_issue:
        # A render that raises is still a refusal, and a refusal that leaves no record is the
        # one thing this system may not do. `NumeralInNarrative` and `TemplateError` are gates
        # firing — correctly, on the artefact rather than on the draft — and they used to
        # escape as a traceback, taking the whole run record with them. The estate then had no
        # account of what it refused or why, which is worse than the defect that caused it.
        try:
            produced = _render(tenant, root, out, results)
        except (NumeralInNarrative, TemplateError, ReportBlocked) as failure:
            console.print("  [red]blocked[/] — rendering refused, no artefact")
            console.print(f"      {failure}")
            record.blockers.append(
                run_record.OmissionRecord(
                    datapoint_id=getattr(failure, "datapoint_id", "?"),
                    reference="",
                    reason_code="E_RESOLVER_ERROR",
                    detail=str(failure),
                    outcome="blocked",
                    lawful=False,
                )
            )
            record.finished_at = dt.datetime.now(dt.UTC)
            written = record.write(out / "runs")
            console.print(f"  recorded {written}")
            console.print(f"[red]FAIL[/] {tenant} could not issue; the record says why")
            return 1
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
def cost_status(
    out: Path = typer.Option(Path("out"), "--out"),
) -> None:
    """What the reports on disk cost, per tenant and per operation.

    Read from the run records rather than from a billing API, and the distinction matters: this
    is what *this system* charged the tenant it was working for, priced per meter at list, and
    attributed to the step that incurred it. A monthly invoice tells you the account spent
    money; this tells you which undertaking's report spent it and on what.

    It used to print "No estate is standing" unconditionally — including with an estate
    standing, which is how it was found. There was nothing behind it to print: the resolver was
    never handed a meter, so every record said `0.0000`.
    """
    records = run_record.RunRecord.load_all(out / "runs")
    if not records:
        _fail(f"no run records under {out / 'runs'}; run `attestor run --tenant <id>` first")

    total = sum(Decimal(record.cost_eur) for record in records)
    issued = sum(1 for record in records if record.issued)

    by_operation: dict[str, Decimal] = {}
    for record in records:
        for operation, amount in record.cost_by_operation.items():
            by_operation[operation] = by_operation.get(operation, Decimal(0)) + Decimal(amount)

    console.print(f"total: EUR {total:.6f} over {len(records)} run(s), {issued} issued")
    console.print("")
    console.print("per tenant:")
    for record in sorted(records, key=lambda r: r.tenant):
        verdict = "issued" if record.issued else "blocked"
        console.print(f"  {record.tenant}: EUR {Decimal(record.cost_eur):.6f}  ({verdict})")

    if by_operation:
        console.print("")
        console.print("per operation:")
        for operation, amount in sorted(by_operation.items(), key=lambda item: -item[1]):
            console.print(f"  {operation}: EUR {amount:.6f}")

    # A blocked run costs money too — it queried the lakehouse and drafted prose before it
    # refused — so the denominator is every run, and the number is what a report costs whether
    # or not one comes out the other end.
    console.print("")
    console.print(f"per run: EUR {(total / len(records)):.6f}")


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
