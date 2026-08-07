"""Assembling a document from a template and a set of resolutions.

Two refusals live here, and both are unconditional.

**A blocked report is not rendered at all.** Not rendered with gaps, not rendered with a
watermark, not rendered into a "draft" directory. If any datapoint blocked, there is no
artefact — because an artefact that exists will eventually be sent, and a draft with a hole
in it looks exactly like a finished report to whoever finds it in a shared folder.

**A figure is formatted from its contract, never from the value's own repr.** Precision is a
disclosure decision the contract makes; `str(Decimal)` is a formatting accident. The two
agree until the day a value arrives with more decimal places than the contract allows, and
on that day the accident wins.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from attestor.contracts.loader import ContractSet
from attestor.contracts.model import DatapointContract
from attestor.contracts.reason_codes import render_disclosure
from attestor.datapoints.resolver import Abstained, ResolutionSet
from attestor.documents.manifest import RenderManifest, RunKind, TextRun
from attestor.documents.template import (
    Block,
    PlaceholderKind,
    Template,
    TemplateError,
    parse_placeholders,
)

#: Column headings, declared once so the renderer and the manifest cannot disagree.
_TABLE_HEADERS: dict[str, tuple[str, ...]] = {
    "datapoints": ("Datapoint", "Reference", "Value", "Lineage"),
    "annex": ("Datapoint", "Value", "Resolver", "Sources", "Lineage"),
    "limitations": ("Datapoint", "Limitation"),
}

#: A retrieval identifier inside model-authored prose, e.g. ``[ev:7f3a]``.
CITATION = re.compile(r"\[ev:[0-9a-z]+\]")

#: A datapoint placeholder the model embedded in its own prose. The prompt requires one
#: wherever a figure belongs, so a narrative legitimately contains these and the renderer
#: has to place the value rather than read the placeholder as text.
EMBEDDED_DATAPOINT = re.compile(r"\{\{dp:([A-Za-z0-9_.\-]+)\}\}")

#: Both, in one pass, so the prose between them is whatever is left.
NARRATIVE_TOKEN = re.compile(
    r"(?P<citation>\[ev:[0-9a-z]+\])|\{\{dp:(?P<datapoint>[A-Za-z0-9_.\-]+)\}\}"
)


class ReportBlocked(RuntimeError):
    """The report cannot be issued, so no artefact is produced."""

    def __init__(self, blockers: tuple[Abstained, ...]) -> None:
        lines = "\n  ".join(f"{a.datapoint_id}: {a.reason_code} — {a.detail}" for a in blockers)
        super().__init__(
            f"{len(blockers)} datapoint(s) block this report; no document was written:\n  {lines}"
        )
        self.blockers = blockers


@dataclass(frozen=True, slots=True)
class RenderContext:
    tenant_id: str
    tenant_name: str
    period: str
    period_start: dt.date
    period_end: dt.date
    report_date: dt.date

    def meta(self, key: str, standard: str) -> str:
        values = {
            "tenant_id": self.tenant_id,
            "tenant_name": self.tenant_name,
            "period": self.period,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "report_date": self.report_date.isoformat(),
            "standard": standard,
        }
        try:
            return values[key]
        except KeyError:  # pragma: no cover — template validation catches this first
            raise TemplateError(f"unknown meta key {key!r}") from None


@dataclass(slots=True)
class RenderedBlock:
    kind: str
    runs: list[TextRun] = field(default_factory=list)
    level: int = 1
    rows: list[list[str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(slots=True)
class RenderedSection:
    title: str
    reference: str
    blocks: list[RenderedBlock] = field(default_factory=list)


@dataclass(slots=True)
class RenderedDocument:
    template: Template
    manifest: RenderManifest
    sections: list[RenderedSection] = field(default_factory=list)


def format_figure(contract: DatapointContract, value: Decimal) -> str:
    """The exact string that appears on the page, decided by the contract."""
    precision = contract.precision or 0
    rendered = f"{value:,.{precision}f}"
    return f"{rendered} {contract.unit}" if contract.unit else rendered


def render(
    template: Template,
    *,
    results: ResolutionSet,
    contracts: ContractSet,
    context: RenderContext,
) -> RenderedDocument:
    """Build a document and its manifest, or refuse."""
    if not results.can_issue:
        raise ReportBlocked(results.blockers)

    manifest = RenderManifest(
        tenant=context.tenant_id,
        period=context.period,
        template_id=template.id,
        artefact=template.artefact,
    )
    document = RenderedDocument(template=template, manifest=manifest)

    _title_block(document, template, context, manifest)

    for section in template.sections:
        rendered = RenderedSection(title=section.title, reference=section.reference)
        heading = RenderedBlock(kind="heading", level=1)
        heading.runs.append(
            manifest.add(
                TextRun(
                    kind=RunKind.STATIC,
                    text=section.title,
                    location=f"{template.id}/{section.title}",
                )
            )
        )
        rendered.blocks.append(heading)
        for block in section.blocks:
            rendered.blocks.extend(
                _render_block(block, template, results, contracts, context, manifest, section.title)
            )
        document.sections.append(rendered)

    return document


# ── Blocks ───────────────────────────────────────────────────────────────────


def _title_block(
    document: RenderedDocument,
    template: Template,
    context: RenderContext,
    manifest: RenderManifest,
) -> None:
    section = RenderedSection(title="", reference="")
    title = RenderedBlock(kind="title")
    title.runs.append(
        manifest.add(TextRun(kind=RunKind.STATIC, text=template.title, location="title"))
    )
    section.blocks.append(title)

    subtitle = RenderedBlock(kind="subtitle")
    subtitle.runs.append(
        manifest.add(
            TextRun(
                kind=RunKind.META,
                text=f"{context.tenant_name} · reporting period {context.period}",
                location="subtitle",
            )
        )
    )
    section.blocks.append(subtitle)
    document.sections.append(section)


def _render_block(
    block: Block,
    template: Template,
    results: ResolutionSet,
    contracts: ContractSet,
    context: RenderContext,
    manifest: RenderManifest,
    where: str,
) -> list[RenderedBlock]:
    location = f"{template.id}/{where}"

    if block.kind == "page_break":
        return [RenderedBlock(kind="page_break")]

    if block.kind == "table":
        return [_render_table(block, results, contracts, manifest, location)]

    if block.kind == "bullets":
        rendered = RenderedBlock(kind="bullets")
        for item in block.items:
            rendered.rows.append(
                [_substitute(item, results, contracts, context, manifest, template, location)]
            )
        return [rendered]

    rendered = RenderedBlock(kind=block.kind, level=block.level)
    rendered.runs.extend(
        _runs_for(block.text, results, contracts, context, manifest, template, location)
    )
    return [rendered]


def _substitute(
    text: str,
    results: ResolutionSet,
    contracts: ContractSet,
    context: RenderContext,
    manifest: RenderManifest,
    template: Template,
    location: str,
) -> str:
    runs = _runs_for(text, results, contracts, context, manifest, template, location)
    return "".join(run.text for run in runs)


def _runs_for(
    text: str,
    results: ResolutionSet,
    contracts: ContractSet,
    context: RenderContext,
    manifest: RenderManifest,
    template: Template,
    location: str,
) -> list[TextRun]:
    """Split a template string into runs, one per placeholder and one per literal between."""
    runs: list[TextRun] = []
    cursor = 0
    for placeholder in parse_placeholders(text):
        start = text.index(placeholder.raw, cursor)
        if start > cursor:
            runs.append(
                manifest.add(
                    TextRun(kind=RunKind.STATIC, text=text[cursor:start], location=location)
                )
            )
        runs.extend(
            _placeholder_runs(
                placeholder, results, contracts, context, manifest, template, location
            )
        )
        cursor = start + len(placeholder.raw)
    if cursor < len(text):
        runs.append(
            manifest.add(TextRun(kind=RunKind.STATIC, text=text[cursor:], location=location))
        )
    return runs


def _placeholder_runs(
    placeholder,
    results: ResolutionSet,
    contracts: ContractSet,
    context: RenderContext,
    manifest: RenderManifest,
    template: Template,
    location: str,
) -> list[TextRun]:
    """Every run one placeholder expands to.

    A list rather than a single run because a narrative expands to several: prose, citation,
    prose, citation. Returning only the last one is a bug this repository actually had — the
    manifest held the whole paragraph while the document received its final fragment, and the
    gate caught it by noticing the manifest described a document that did not exist.
    """
    reference = placeholder.reference

    if placeholder.kind is PlaceholderKind.META:
        return [
            manifest.add(
                TextRun(
                    kind=RunKind.META,
                    text=context.meta(reference, template.standard),
                    location=location,
                )
            )
        ]

    contract = contracts.get(reference)
    if contract is None:
        raise TemplateError(f"{location}: no contract for datapoint {reference!r}")
    outcome = results.get(reference)
    if outcome is None:
        raise TemplateError(f"{location}: {reference} was never resolved")

    if isinstance(outcome, Abstained):
        return [
            manifest.add(
                TextRun(
                    kind=RunKind.LIMITATION,
                    text=_omission_text(outcome, contract),
                    datapoint_id=reference,
                    location=location,
                )
            )
        ]

    if placeholder.kind is PlaceholderKind.NARRATIVE:
        if outcome.narrative is None:
            raise TemplateError(f"{location}: {reference} resolved without narrative text")
        return _narrative_runs(outcome, reference, manifest, location, results, contracts)

    return [
        manifest.add(
            TextRun(
                kind=RunKind.FIGURE,
                text=format_figure(contract, outcome.value),
                datapoint_id=reference,
                lineage_id=outcome.lineage.lineage_id,
                location=location,
            )
        )
    ]


def _narrative_runs(
    outcome,
    reference: str,
    manifest: RenderManifest,
    location: str,
    results,
    contracts,
) -> list[TextRun]:
    """Split model-authored prose from the citation markers embedded in it.

    The markers are identifiers the retriever minted, not text the model composed, so they
    are recorded as CITATION runs. Without this split every `[ev:7f3a]` would read as a model
    writing digits, and the narrative rule would have to be softened to accommodate it — which
    is exactly the kind of softening that makes a control stop meaning anything.

    A marker the model quoted but did not declare in its citation list is refused: it is
    either a hallucinated source or an attempt to smuggle characters past the rule.
    """
    text = outcome.narrative
    declared = {c.split(":", 1)[-1] for c in outcome.citations}
    runs: list[TextRun] = []
    cursor = 0

    def prose(fragment: str) -> None:
        if fragment:
            runs.append(
                manifest.add(
                    TextRun(
                        kind=RunKind.NARRATIVE,
                        text=fragment,
                        datapoint_id=reference,
                        lineage_id=outcome.lineage.lineage_id,
                        location=location,
                    )
                )
            )

    for match in NARRATIVE_TOKEN.finditer(text):
        prose(text[cursor : match.start()])

        marker = match.group("citation")
        if marker is not None:
            if marker.strip("[]").split(":", 1)[1] not in declared:
                raise TemplateError(
                    f"{location}: {reference} cites {marker} which is not in its declared "
                    "citations — a quoted source must be one the retriever returned"
                )
            runs.append(
                manifest.add(
                    TextRun(
                        kind=RunKind.CITATION,
                        text=marker,
                        datapoint_id=reference,
                        lineage_id=outcome.lineage.lineage_id,
                        location=location,
                    )
                )
            )
            cursor = match.end()
            continue

        # A datapoint placeholder the model embedded in its prose. This is the whole point of
        # the mechanism: the model marks where a figure belongs and the resolver puts one
        # there, carrying that datapoint's own lineage rather than the narrative's. Treating
        # it as prose is what made a correct draft fail the numeral rule on the digits inside
        # `{{dp:ESRS_E1-6_gross_scope_1}}` — the renderer refusing the very thing the prompt
        # demands.
        embedded = match.group("datapoint")
        contract = contracts.get(embedded)
        if contract is None:
            raise TemplateError(
                f"{location}: {reference} places {embedded!r}, which is not a datapoint. A "
                "model may mark where a figure goes; it may not invent what goes there"
            )
        placed = results.get(embedded)
        if placed is None:
            raise TemplateError(
                f"{location}: {reference} places {embedded}, which was never resolved"
            )
        if isinstance(placed, Abstained):
            runs.append(
                manifest.add(
                    TextRun(
                        kind=RunKind.LIMITATION,
                        text=_omission_text(placed, contract),
                        datapoint_id=embedded,
                        location=location,
                    )
                )
            )
        else:
            runs.append(
                manifest.add(
                    TextRun(
                        kind=RunKind.FIGURE,
                        text=format_figure(contract, placed.value),
                        datapoint_id=embedded,
                        lineage_id=placed.lineage.lineage_id,
                        location=location,
                    )
                )
            )
        cursor = match.end()

    prose(text[cursor:])
    if not runs:  # pragma: no cover — an empty narrative is refused upstream
        raise TemplateError(f"{location}: {reference} produced no narrative runs")
    return runs


def _omission_text(outcome: Abstained, contract: DatapointContract) -> str:
    """What stands in place of a figure. Never model-authored, never free text."""
    if outcome.override is not None:
        return outcome.override.render_limitation(reference=contract.reference)
    return render_disclosure(
        outcome.reason_code, datapoint=contract.id, reference=contract.reference
    )


def _render_table(
    block: Block,
    results: ResolutionSet,
    contracts: ContractSet,
    manifest: RenderManifest,
    location: str,
) -> RenderedBlock:
    """Build a table, registering every cell.

    Every cell goes through `cell()`, which records it in the manifest and returns the string
    it wrote. That is not ceremony: an earlier version registered only the value cells, and
    the gate correctly failed the annex because "Gross Scope 1 greenhouse gas emissions" put
    a digit on the page that no run accounted for. Rows and manifest are now the same object
    seen twice, so they cannot drift.
    """
    rendered = RenderedBlock(kind="table")

    def cell(
        kind: RunKind, text: str, *, datapoint_id: str | None = None, lineage_id: str | None = None
    ) -> str:
        manifest.add(
            TextRun(
                kind=kind,
                text=text,
                datapoint_id=datapoint_id,
                lineage_id=lineage_id,
                location=f"{location}/{block.table}",
            )
        )
        return text

    for header in _TABLE_HEADERS[block.table]:
        cell(RunKind.STATIC, header)
    rendered.headers = list(_TABLE_HEADERS[block.table])

    match block.table:
        case "datapoints":
            for result in sorted(results.published, key=lambda r: r.datapoint_id):
                contract = contracts[result.datapoint_id]
                if contract.is_model_authored:
                    continue
                rendered.rows.append(
                    [
                        # The title and the clause reference come from a committed contract,
                        # reviewed in a pull request — the same provenance as template text.
                        cell(RunKind.STATIC, contract.title),
                        cell(RunKind.STATIC, contract.reference),
                        cell(
                            RunKind.FIGURE,
                            format_figure(contract, result.value),
                            datapoint_id=result.datapoint_id,
                            lineage_id=result.lineage.lineage_id,
                        ),
                        cell(RunKind.META, result.lineage.short_id),
                    ]
                )

        case "annex":
            for row in results.ledger.as_annex():
                # `assurance.auditor_annex` decides whether a datapoint's full lineage is put
                # in front of the auditor. Every contract declared it and nothing read it, so
                # a datapoint marked `auditor_annex: false` appeared in the annex anyway —
                # the field was documentation of an intention, not a control over output.
                contract = contracts.get(row["datapoint"])
                if contract is not None and not contract.assurance.auditor_annex:
                    continue
                rendered.rows.append(
                    [
                        cell(RunKind.META, row["datapoint"], datapoint_id=row["datapoint"]),
                        cell(
                            RunKind.META,
                            f"{row['value']} {row['unit']}" if row["value"] else "\u2014",
                            datapoint_id=row["datapoint"],
                        ),
                        cell(RunKind.META, row["resolver"], datapoint_id=row["datapoint"]),
                        cell(
                            RunKind.META,
                            ", ".join(row["sources"]) or "\u2014",
                            datapoint_id=row["datapoint"],
                        ),
                        cell(RunKind.META, row["lineage"], datapoint_id=row["datapoint"]),
                    ]
                )

        case "limitations":
            for abstention in sorted(results.limitations, key=lambda a: a.datapoint_id):
                contract = contracts[abstention.datapoint_id]
                rendered.rows.append(
                    [
                        cell(
                            RunKind.LIMITATION,
                            abstention.datapoint_id,
                            datapoint_id=abstention.datapoint_id,
                        ),
                        cell(
                            RunKind.LIMITATION,
                            _omission_text(abstention, contract),
                            datapoint_id=abstention.datapoint_id,
                        ),
                    ]
                )
            if not rendered.rows:
                rendered.rows.append(
                    [
                        cell(RunKind.LIMITATION, "\u2014"),
                        cell(
                            RunKind.LIMITATION,
                            "No material limitations were recorded for this period.",
                        ),
                    ]
                )

    return rendered
