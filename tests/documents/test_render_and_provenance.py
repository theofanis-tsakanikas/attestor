"""The document factory and the gate that inspects what it produced.

The most important test in this file is the last one: it takes a genuinely rendered Word
document, injects a number into it the way a careless edit or a compromised narrative would,
and requires the gate to catch it. A gate that has never been shown to fail is a comment.
"""

from __future__ import annotations

import datetime as dt
import shutil
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.datapoints.backends import RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import NarrativeDraft, ResolutionContext, Resolver
from attestor.documents import render as render_module
from attestor.documents import writers
from attestor.documents.manifest import (
    NumeralInNarrative,
    RenderManifest,
    RunKind,
    TextRun,
)
from attestor.documents.render import RenderContext, ReportBlocked, format_figure
from attestor.documents.template import Template
from attestor.gates import provenance

PERIOD_START = dt.date(2026, 1, 1)
PERIOD_END = dt.date(2027, 1, 1)
REPORT_DATE = dt.date(2026, 7, 1)


def _narrative(_contract, _context) -> NarrativeDraft:
    return NarrativeDraft(
        text=(
            "The undertaking has a board-approved transition plan covering its owned fleet "
            "and leased depots. [ev:7f3a] Decarbonisation relies on fleet replacement and "
            "site electrification. [ev:91c0] Board approval is evidenced in the minutes. "
            "[ev:2d55]"
        ),
        citations=("ev:7f3a", "ev:91c0", "ev:2d55"),
        prompt_ref="esrs_e1_1_transition_plan@3",
    )


@pytest.fixture
def resolved(repo_root: Path, contract_set: ContractSet):
    resolver = Resolver(
        contracts=contract_set,
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=_narrative,
    )
    return resolver.resolve_all(
        ResolutionContext(
            tenant="helios",
            period="2026",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            as_of=REPORT_DATE,
            run_id="render-test",
        )
    )


@pytest.fixture
def context() -> RenderContext:
    return RenderContext(
        tenant_id="helios",
        tenant_name="Helios Logistics S.A.",
        period="2026",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        report_date=REPORT_DATE,
    )


@pytest.fixture
def statement(repo_root: Path) -> Template:
    return Template.load(repo_root / "templates" / "esrs" / "sustainability_statement.yaml")


# ── Templates ────────────────────────────────────────────────────────────────


def test_every_committed_template_loads(repo_root: Path) -> None:
    templates = Template.load_all(repo_root / "templates")
    assert {t.artefact for t in templates} == {"docx", "xlsx", "pptx"}


def test_every_placeholder_names_a_real_datapoint(
    repo_root: Path, contract_set: ContractSet
) -> None:
    for template in Template.load_all(repo_root / "templates"):
        for datapoint_id in template.datapoints():
            assert datapoint_id in contract_set, f"{template.id} → {datapoint_id}"


def test_a_mistyped_placeholder_is_a_load_error() -> None:
    """It must not print `{{dp:ESRS_E1-6_gros_scope_1}}` into a filing."""
    with pytest.raises(ValidationError, match="not a placeholder"):
        Template.model_validate(
            {
                "id": "broken",
                "artefact": "docx",
                "title": "Broken",
                "standard": "ESRS",
                "sections": [
                    {
                        "title": "S",
                        "blocks": [{"kind": "paragraph", "text": "value {{dp ESRS_X_y}}"}],
                    }
                ],
            }
        )


def test_an_unknown_meta_key_is_a_load_error() -> None:
    with pytest.raises(ValidationError, match="unknown meta key"):
        Template.model_validate(
            {
                "id": "broken_meta",
                "artefact": "docx",
                "title": "Broken",
                "standard": "ESRS",
                "sections": [
                    {"title": "S", "blocks": [{"kind": "paragraph", "text": "{{meta:ceo_mood}}"}]}
                ],
            }
        )


# ── Formatting ───────────────────────────────────────────────────────────────


def test_a_figure_is_formatted_by_its_contract(contract_set: ContractSet) -> None:
    contract = contract_set["ESRS_E1-6_gross_scope_1"]
    assert format_figure(contract, Decimal("18422")) == "18,422 tCO2e"


def test_precision_comes_from_the_contract_not_the_value(contract_set: ContractSet) -> None:
    """`str(Decimal)` and the contract agree until they do not, and then the accident wins."""
    contract = contract_set["ESRS_E1-6_net_revenue"]
    assert format_figure(contract, Decimal("486.2")) == "486.20 MEUR"


# ── Rendering ────────────────────────────────────────────────────────────────


def test_the_statement_renders(statement: Template, resolved, contract_set, context) -> None:
    document = render_module.render(
        statement, results=resolved, contracts=contract_set, context=context
    )
    assert document.manifest.figures
    assert document.manifest.of_kind(RunKind.NARRATIVE)


def test_a_blocked_report_produces_no_artefact(
    repo_root: Path, contract_set: ContractSet, statement: Template, context: RenderContext
) -> None:
    """Not a draft, not a watermark. Nothing."""
    resolver = Resolver(
        contracts=contract_set,
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "aegis"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=_narrative,
    )
    blocked = resolver.resolve_all(
        ResolutionContext(
            tenant="aegis",
            period="2026",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            as_of=REPORT_DATE,
        )
    )
    with pytest.raises(ReportBlocked, match="no document was written"):
        render_module.render(statement, results=blocked, contracts=contract_set, context=context)


def test_every_figure_run_carries_a_lineage_id(
    statement: Template, resolved, contract_set, context
) -> None:
    document = render_module.render(
        statement, results=resolved, contracts=contract_set, context=context
    )
    for run in document.manifest.figures:
        assert run.lineage_id, run.datapoint_id


# ── The narrative rule ───────────────────────────────────────────────────────


def test_a_narrative_carrying_a_digit_is_refused_at_manifest_time() -> None:
    manifest = RenderManifest(tenant="helios", period="2026", template_id="t", artefact="docx")
    with pytest.raises(NumeralInNarrative, match="never written by a model"):
        manifest.add(
            TextRun(
                kind=RunKind.NARRATIVE,
                text="Emissions fell to 18,422 tCO2e this year.",
                datapoint_id="ESRS_E1-1_transition_plan",
            )
        )


def test_a_model_that_writes_a_number_fails_the_render(
    repo_root: Path, contract_set: ContractSet, statement: Template, context: RenderContext
) -> None:
    """End to end: a compromised narrative provider cannot get a figure onto the page."""

    def chatty(_contract, _context) -> NarrativeDraft:
        return NarrativeDraft(
            text="Our emissions were 18,422 tCO2e. [ev:1] [ev:2] [ev:3]",
            citations=("ev:1", "ev:2", "ev:3"),
            prompt_ref="p@1",
        )

    resolver = Resolver(
        contracts=contract_set,
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=chatty,
    )
    results = resolver.resolve_all(
        ResolutionContext(
            tenant="helios",
            period="2026",
            period_start=PERIOD_START,
            period_end=PERIOD_END,
            as_of=REPORT_DATE,
        )
    )
    with pytest.raises(NumeralInNarrative):
        render_module.render(statement, results=results, contracts=contract_set, context=context)


# ── The gate, against real files ─────────────────────────────────────────────


@pytest.fixture
def artefacts(tmp_path: Path, repo_root: Path, resolved, contract_set, context):
    written = []
    for template in Template.load_all(repo_root / "templates"):
        document = render_module.render(
            template, results=resolved, contracts=contract_set, context=context
        )
        path = tmp_path / f"{template.id}.{template.artefact}"
        writers.write(document, path)
        writers.write_manifest(document, path.with_suffix(f".{template.artefact}.manifest.json"))
        written.append((path, document.manifest))
    return written


def test_all_three_artefacts_are_written(artefacts) -> None:
    assert {path.suffix for path, _ in artefacts} == {".docx", ".xlsx", ".pptx"}
    for path, _ in artefacts:
        assert path.stat().st_size > 0


@pytest.mark.gate
def test_a_clean_render_passes_the_provenance_gate(artefacts) -> None:
    """Green first. Every mutation below is meaningless otherwise."""
    results = provenance.check_all(artefacts)
    for result in results:
        assert result.passed, result.summary() + "\n" + "\n".join(str(f) for f in result.findings)
    assert sum(r.numerals_checked for r in results) > 0


@pytest.mark.gate
def test_the_gate_catches_a_number_injected_into_a_finished_document(artefacts) -> None:
    """The mutation that matters: a figure nobody resolved, added after rendering."""
    docx, manifest = next((p, m) for p, m in artefacts if p.suffix == ".docx")
    tampered = docx.with_name("tampered.docx")
    shutil.copy(docx, tampered)

    with zipfile.ZipFile(docx) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    body = entries["word/document.xml"].decode("utf-8")
    entries["word/document.xml"] = body.replace(
        "</w:body>",
        "<w:p><w:r><w:t>Adjusted total: 91,337 tCO2e</w:t></w:r></w:p></w:body>",
    ).encode("utf-8")
    with zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as out:
        for name, payload in entries.items():
            out.writestr(name, payload)

    result = provenance.check(tampered, manifest)
    assert not result.passed
    assert any(f.kind == "unaccounted-numeral" for f in result.findings)
    assert any("91,337" in f.detail for f in result.findings)


@pytest.mark.gate
def test_the_gate_reads_speaker_notes_and_footers_too(artefacts) -> None:
    """A number smuggled into a footer is still a number in a filing."""
    pptx, manifest = next((p, m) for p, m in artefacts if p.suffix == ".pptx")
    text = provenance.extract_text(pptx)
    assert text.strip()
    assert provenance.check(pptx, manifest).passed


@pytest.mark.gate
def test_the_gate_notices_a_manifest_that_does_not_describe_the_document(artefacts) -> None:
    """A manifest is a claim about an artefact, not a substitute for reading it."""
    docx, manifest = next((p, m) for p, m in artefacts if p.suffix == ".docx")
    fabricated = manifest.model_copy(deep=True)
    fabricated.runs.append(
        TextRun(
            kind=RunKind.FIGURE,
            text="4,242,424 tCO2e",
            datapoint_id="ESRS_E1-6_total_ghg",
            lineage_id="deadbeefcafe",
        )
    )
    result = provenance.check(docx, fabricated)
    assert not result.passed
    assert any(f.kind == "figure-not-in-artefact" for f in result.findings)
