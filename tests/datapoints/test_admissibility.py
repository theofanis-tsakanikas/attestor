"""The control that keeps a misread digit out of a published figure.

An OCR engine reading `1` as `7` produces a number that is plausible, well-formed and wrong.
No confidence threshold fixes that — the misreads that matter are the ones the reader was
sure about. So the question is answered structurally: an extracted value may support a
published figure only where the contract declares a `tolerance.cross_check` that reconciles
it against an independent path, and only from the side of that reconciliation which is not
itself paper.

These assert the rule against the real contract set, and then assert that when the rule is
satisfied and the reading is *still* wrong, the existing refusal machinery does its job.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from attestor.agent.narrative import RecordedNarrativeProvider
from attestor.contracts import loader, overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.datapoints.admissibility import (
    InadmissibleExtraction,
    admissible_datapoints,
    judge,
    require_admissible,
)
from attestor.datapoints.backends import QueryResult, RecordedBackend
from attestor.datapoints.evidence import EvidenceDocument, EvidenceIndex
from attestor.datapoints.extraction import (
    ExtractedField,
    Extraction,
    ExtractionUnavailable,
    RecordedExtractor,
    RowSpec,
    to_rows,
)
from attestor.datapoints.resolver import Abstained, ResolutionContext, Resolved, Resolver

SCOPE_1 = "ESRS_E1-6_gross_scope_1"
FUEL_SPEND = "gold.procurement_fuel_spend"
TELEMATICS = "gold.ghg_scope_1_activity"

FUEL_SPEC = RowSpec(
    dataset=FUEL_SPEND,
    document_class="fuel_invoice",
    columns={
        "invoice_date": "invoice_date",
        "fuel_type": "fuel_type",
        "net_amount_eur": "net_amount_eur",
    },
    numeric=frozenset({"net_amount_eur"}),
)


def _document(root: Path, tenant: str, document_id: str) -> EvidenceDocument:
    for document in EvidenceIndex.for_tenant(root, tenant):
        if document.document_id == document_id:
            return document
    raise AssertionError(document_id)


def _context(tenant: str = "helios") -> ResolutionContext:
    return ResolutionContext(
        tenant=tenant,
        period="2026",
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2027, 1, 1),
        as_of=dt.date(2026, 7, 1),
    )


# ── The rule, against the real contracts ─────────────────────────────────────


def test_extracted_rows_may_back_a_cross_check(contract_set: ContractSet) -> None:
    verdict = judge(contract_set[SCOPE_1], dataset=FUEL_SPEND, contracts=contract_set)
    assert verdict.admissible
    assert "fuel_spend" in verdict.reconciled_by


def test_extracted_rows_may_not_back_the_primary_resolver(contract_set: ContractSet) -> None:
    """The direction is the control. Reconciling a reading of the paper against the same
    reading proves the reader is consistent, not that the figure is right."""
    verdict = judge(contract_set[SCOPE_1], dataset=TELEMATICS, contracts=contract_set)
    assert not verdict.admissible
    assert "cross-check side" in verdict.reason


def test_a_datapoint_with_no_cross_check_admits_nothing(contract_set: ContractSet) -> None:
    scope_3 = contract_set["ESRS_E1-6_gross_scope_3"]
    assert not scope_3.tolerance.cross_check
    verdict = judge(scope_3, dataset="gold.ghg_scope_3_activity", contracts=contract_set)
    assert not verdict.admissible
    assert "no cross-check" in verdict.reason


def test_a_narrative_datapoint_admits_nothing(contract_set: ContractSet) -> None:
    verdict = judge(
        contract_set["ESRS_E1-1_transition_plan"], dataset=FUEL_SPEND, contracts=contract_set
    )
    assert not verdict.admissible


def test_requiring_admissibility_refuses_an_unreconciled_dataset(
    contract_set: ContractSet,
) -> None:
    with pytest.raises(InadmissibleExtraction, match="evidence coverage and nothing more"):
        require_admissible("gold.ghg_scope_3_activity", contract_set)


def test_every_dataset_the_ingest_writes_is_reconciled(repo_root: Path) -> None:
    """The specs in the pipeline and the rule here must agree, or the ingest writes rows
    into a figure nothing checks."""
    spec = importlib.util.spec_from_file_location(
        "attestor_ingest_adm", repo_root / "pipelines" / "ingest" / "evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["attestor_ingest_adm"] = module
    spec.loader.exec_module(module)

    contracts = loader.load(repo_root)
    for row_spec in module.ROW_SPECS:
        assert require_admissible(row_spec.dataset, contracts)


def test_the_admissible_set_is_small_and_named(contract_set: ContractSet) -> None:
    """If this grows, somebody has widened what paper may become. That should be a diff."""
    assert admissible_datapoints(FUEL_SPEND, contract_set) == (SCOPE_1,)
    assert admissible_datapoints(TELEMATICS, contract_set) == ()


# ── A misread digit, all the way to a refusal ────────────────────────────────


class _Corrupted(RecordedBackend):
    """Replays the recordings, except the cross-check, which reflects extracted rows.

    This is the shape of the real failure: telematics says one thing, the invoice was read
    wrong, and the two no longer reconcile. The resolver is the production one.
    """

    def __init__(self, recordings, crosscheck: Decimal | None) -> None:
        super().__init__(recordings)
        self._crosscheck = crosscheck

    # `**passthrough`, not a fixed signature. The resolver also hands the backend the
    # as-of pins now, and a stub that names every argument stops receiving the call the
    # moment one is added — silently, as an empty capture rather than a TypeError.
    def execute(self, *, sql, parameters, **passthrough):
        result = super().execute(sql=sql, parameters=parameters, **passthrough)
        if "procurement_fuel_spend" in sql:
            return QueryResult(
                value=self._crosscheck,
                tables=result.tables,
                snapshot_ids=result.snapshot_ids,
                quarantined_rows=result.quarantined_rows,
            )
        return result


def _resolve(repo_root: Path, contract_set: ContractSet, backend) -> object:
    resolver = Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=backend,
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=RecordedNarrativeProvider.from_root(repo_root),
    )
    return resolver.resolve_all(_context())


def test_an_ocr_digit_error_blocks_issuance_with_the_right_reason(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """`1` read as `7` in the fuel invoice. The figure is plausible and the cross-check
    refuses it — which is the entire reason extracted values are only allowed here."""
    recordings = RecordedBackend.from_directory(repo_root / "recordings")._recordings
    # helios' primary is 18422.4118 and its recorded cross-check 18485.0, inside the 0.5%
    # bound. A single misread digit on a quarterly invoice moves the cross-check well past it.
    results = _resolve(repo_root, contract_set, _Corrupted(recordings, Decimal("21118.0")))

    outcome = results[SCOPE_1]
    assert isinstance(outcome, Abstained)
    assert outcome.reason_code == "E_OUT_OF_TOLERANCE"
    assert outcome.blocks_report
    assert not results.can_issue


def test_a_correct_reading_still_issues(repo_root: Path, contract_set: ContractSet) -> None:
    """The control must not simply refuse everything — that scores perfectly and is useless."""
    recordings = RecordedBackend.from_directory(repo_root / "recordings")._recordings
    results = _resolve(repo_root, contract_set, _Corrupted(recordings, Decimal("18485.0")))

    assert isinstance(results[SCOPE_1], Resolved)
    assert results.can_issue


def test_a_failed_extraction_abstains_rather_than_publishing_a_partial_figure(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """Extraction runs at ingest, so a failure leaves no rows and the cross-check finds
    nothing. What must not happen is a figure computed over whatever survived."""
    recordings = RecordedBackend.from_directory(repo_root / "recordings")._recordings
    results = _resolve(repo_root, contract_set, _Corrupted(recordings, None))

    outcome = results[SCOPE_1]
    assert isinstance(outcome, Abstained)
    assert outcome.reason_code == "E_OUT_OF_TOLERANCE"
    assert "matched no rows" in outcome.detail
    assert not results.can_issue


# ── Quarantine, counted rather than dropped ──────────────────────────────────


def test_an_unparseable_amount_quarantines_the_row_and_keeps_it(repo_root: Path) -> None:
    """Dropping it would let the figure shrink quietly. Keeping it marked is what makes
    `E_UPSTREAM_QUARANTINE` reachable."""
    document = _document(repo_root, "helios", "FUEL-HEL-2026-Q1")
    read = RecordedExtractor.from_root(repo_root).extract(document)
    misread = Extraction(
        document_id=document.document_id,
        content_sha256=document.content_sha256,
        pages=read.pages,
        fields=(
            *(f for f in read.fields if f.name != "net_amount_eur"),
            # `4` read as `l`, `0` as `O`. A confident reader, a well-formed string, and a
            # value no parser should accept.
            ExtractedField(name="net_amount_eur", value="4l2,86O.55", confidence=0.51, page=14),
        ),
    )

    built = to_rows(misread, document, FUEL_SPEC)
    assert built.quarantined_rows == 1
    assert built.clean == ()
    assert built.rows[0]["dq_rule"].startswith("unparseable:")
    assert built.rows[0]["source_document_id"] == document.document_id


def test_a_missing_field_quarantines_rather_than_omitting(repo_root: Path) -> None:
    document = _document(repo_root, "helios", "FUEL-HEL-2026-Q1")
    empty = Extraction(document.document_id, document.content_sha256, pages=1)

    built = to_rows(empty, document, FUEL_SPEC)
    assert built.quarantined_rows == 1
    assert built.rows[0]["dq_rule"].startswith("missing_field:")


def test_a_clean_reading_carries_the_document_it_came_from(repo_root: Path) -> None:
    """Lineage does not stop at the lake. A figure over extracted rows must be walkable back
    to the page it was read from."""
    document = _document(repo_root, "helios", "FUEL-HEL-2026-Q1")
    extraction = RecordedExtractor.from_root(repo_root).extract(document)

    built = to_rows(extraction, document, FUEL_SPEC)
    assert built.quarantined_rows == 0
    assert built.clean[0]["source_document_id"] == document.document_id
    assert built.clean[0]["dq_status"] == "clean"


def test_a_document_of_the_wrong_class_is_refused(repo_root: Path) -> None:
    document = _document(repo_root, "helios", "UTIL-HEL-2026-01")
    extraction = RecordedExtractor.from_root(repo_root).extract(document)
    with pytest.raises(Exception, match="built from fuel_invoice"):
        to_rows(extraction, document, FUEL_SPEC)


def test_a_tenant_with_no_captures_builds_no_rows(repo_root: Path) -> None:
    document = _document(repo_root, "lumen", "MODELCARD-ATT-2026")
    with pytest.raises(ExtractionUnavailable):
        RecordedExtractor.from_root(repo_root).extract(document)
