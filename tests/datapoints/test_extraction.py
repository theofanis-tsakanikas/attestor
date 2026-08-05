"""Reading paper — the replay discipline, the metering, and the client that must not be called.

A scanned invoice is a genuine document supporting a genuine disclosure, and until the
extractor existed it produced nothing at all. These cover the half of that which can be
asserted without a figure in sight; the half where an extracted value tries to become a
published number is `tests/datapoints/test_admissibility.py`.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from attestor.datapoints.evidence import EvidenceDocument, EvidenceIndex
from attestor.datapoints.extraction import (
    BedrockDataAutomationExtractor,
    Extraction,
    ExtractionError,
    ExtractionUnavailable,
    MeteredExtractor,
    RecordedExtractor,
    StaleExtraction,
    build,
    parse_output,
)
from attestor.observability.cost import CostMeter, Meter

FUEL = "FUEL-HEL-2026-Q1"


def _document(root: Path, tenant: str, document_id: str) -> EvidenceDocument:
    for document in EvidenceIndex.for_tenant(root, tenant):
        if document.document_id == document_id:
            return document
    raise AssertionError(f"{document_id} is not in {tenant}'s manifest")


@pytest.fixture
def helios_fuel(repo_root: Path) -> EvidenceDocument:
    return _document(repo_root, "helios", FUEL)


# ── Replay is replay ─────────────────────────────────────────────────────────


def test_the_same_document_extracts_identically_twice(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    """Determinism is the whole basis of replaying a reading rather than repeating it."""
    extractor = RecordedExtractor.from_root(repo_root)
    first = extractor.extract(helios_fuel)
    second = extractor.extract(helios_fuel)
    assert first == second
    assert first.pages > 0
    assert first.require("net_amount_eur").as_decimal() > 0


def test_a_capture_taken_from_different_bytes_raises(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    """The dangerous absence: the document changed and the reading did not.

    A silent replay here would describe paper nobody is holding — the same failure
    `StaleRecording` exists for on the query side.
    """
    extractor = RecordedExtractor.from_root(repo_root)
    moved = helios_fuel.model_copy(update={"content_sha256": "f" * 64})
    with pytest.raises(StaleExtraction, match="Re-capture"):
        extractor.extract(moved)


def test_a_document_with_no_capture_is_unavailable_not_invented(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    with pytest.raises(ExtractionUnavailable, match="never invented"):
        RecordedExtractor({}).extract(helios_fuel)


def test_every_capture_names_a_document_that_exists(repo_root: Path) -> None:
    """A reading of a document nobody declares describes nothing."""
    extractor = RecordedExtractor.from_root(repo_root)
    declared = {
        document.document_id
        for tenant in ("helios", "aegis", "lumen")
        for document in EvidenceIndex.for_tenant(repo_root, tenant)
    }
    captured = {entry["document_id"] for entry in extractor._captures.values()}
    assert captured <= declared


def test_every_capture_matches_its_documents_current_digest(repo_root: Path) -> None:
    """Committed captures must not be stale against the committed manifests."""
    extractor = RecordedExtractor.from_root(repo_root)
    by_id = {
        document.document_id: document
        for tenant in ("helios", "aegis", "lumen")
        for document in EvidenceIndex.for_tenant(repo_root, tenant)
    }
    for entry in extractor._captures.values():
        document = by_id[entry["document_id"]]
        assert entry["content_sha256"] == document.content_sha256, entry["document_id"]


# ── Offline calls nothing ────────────────────────────────────────────────────


def test_offline_touches_no_client(repo_root: Path, monkeypatch) -> None:
    """Asserted, not assumed. A run that can reach a paid service by forgetting a flag is
    a run that will."""

    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"offline extraction called client.{name}")

    monkeypatch.delenv("ATTESTOR_EXTRACTOR", raising=False)
    extractor = build(repo_root)
    assert isinstance(extractor, RecordedExtractor)

    # And the live extractor, if one were somehow constructed, would have to go through a
    # client — so proving the recorded one never does is proving the boundary.
    monkeypatch.setattr(RecordedExtractor, "_client", Exploding(), raising=False)
    extractor.extract(_document(repo_root, "helios", FUEL))


def test_the_live_extractor_asserts_its_configuration(repo_root: Path) -> None:
    with pytest.raises(ExtractionError, match="unset"):
        build(repo_root, extractor="bda")


def test_an_explicit_opt_in_is_required_for_the_live_path(repo_root: Path, monkeypatch) -> None:
    monkeypatch.setenv("ATTESTOR_EXTRACTOR", "bda")
    with pytest.raises(ExtractionError):
        build(repo_root)
    monkeypatch.setenv("ATTESTOR_EXTRACTOR", "recorded")
    assert isinstance(build(repo_root), RecordedExtractor)


# ── Metering ─────────────────────────────────────────────────────────────────


def test_pages_are_charged_to_the_tenant_that_owns_the_document(repo_root: Path) -> None:
    meter = CostMeter()
    extractor = MeteredExtractor(RecordedExtractor.from_root(repo_root), meter)
    extraction = extractor.extract(_document(repo_root, "helios", FUEL))

    assert [charge.meter for charge in meter.charges] == [Meter.DOCUMENT_PARSE]
    assert meter.charges[0].quantity == extraction.pages
    assert set(meter.by_tenant()) == {"helios"}


def test_one_tenants_pages_never_land_on_another(repo_root: Path) -> None:
    """The failure this prevents is silent: a per-tenant cost that is wrong in the one
    direction nobody checks."""
    meter = CostMeter()
    extractor = MeteredExtractor(RecordedExtractor.from_root(repo_root), meter)
    extractor.extract(_document(repo_root, "helios", FUEL))
    extractor.extract(_document(repo_root, "aegis", "FUEL-AEG-2026-FY"))

    totals = meter.by_tenant()
    assert set(totals) == {"aegis", "helios"}
    assert all(amount > 0 for amount in totals.values())


def test_a_zero_page_extraction_is_not_a_charge(repo_root: Path) -> None:
    class Empty:
        def extract(self, document: EvidenceDocument) -> Extraction:
            return Extraction(document.document_id, document.content_sha256, pages=0)

    meter = CostMeter()
    MeteredExtractor(Empty(), meter).extract(_document(repo_root, "helios", FUEL))
    assert meter.charges == []


# ── The live extractor, driven through its real path ─────────────────────────


class StubBda:
    """Just enough Bedrock Data Automation to drive `extract` end to end."""

    def __init__(self, states: list[str], output: str = "s3://bucket/out.json") -> None:
        self.states = states
        self.output = output
        self.invoked: dict[str, Any] = {}
        self.stopped = False

    def invoke_data_automation_async(self, **kwargs: Any) -> dict[str, Any]:
        self.invoked = kwargs
        return {"invocationArn": "arn:aws:bda:eu-central-1:1:invocation/x"}

    def get_data_automation_status(self, **_kwargs: Any) -> dict[str, Any]:
        state = self.states.pop(0) if self.states else "Success"
        return {
            "status": state,
            "outputConfiguration": {"s3Uri": self.output},
            "errorMessage": "the page could not be rasterised",
        }


class StubS3:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.read: dict[str, Any] = {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.read = kwargs
        return {"Body": _Body(self.body)}


class _Body:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload


OUTPUT = b"""{
  "metadata": {"number_of_pages": 4},
  "document": {"representation": {"text": "Invoice for diesel."}},
  "inference_result": {
    "net_amount_eur": {"value": "1234.56", "confidence": 0.97, "page": 4},
    "fuel_type": {"value": "diesel", "confidence": 0.99, "page": 1}
  }
}"""


def _live(client: Any, s3: Any, **kwargs: Any) -> BedrockDataAutomationExtractor:
    return BedrockDataAutomationExtractor(
        bucket="evidence",
        project_arn="arn:aws:bedrock:eu-central-1:1:data-automation-project/p",
        client=client,
        s3=s3,
        clock=kwargs.pop("clock", lambda: 0.0),
        sleep=kwargs.pop("sleep", lambda _s: None),
        **kwargs,
    )


def test_the_job_is_polled_to_a_terminal_state(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    client = StubBda(["Created", "InProgress", "InProgress", "Success"])
    extraction = _live(client, StubS3(OUTPUT)).extract(helios_fuel)

    assert extraction.pages == 4
    assert extraction.require("net_amount_eur").as_decimal() == Decimal("1234.56")
    assert extraction.text == "Invoice for diesel."
    assert client.states == []


def test_a_failed_job_surfaces_its_reason(repo_root: Path, helios_fuel: EvidenceDocument) -> None:
    client = StubBda(["InProgress", "ServiceError"])
    with pytest.raises(ExtractionError, match="rasterised"):
        _live(client, StubS3(OUTPUT)).extract(helios_fuel)


def test_a_job_that_never_settles_is_abandoned(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    """Injected clock, so the timeout is a test rather than a test that waits."""
    ticks = iter([0.0, 0.0, 1e9, 1e9, 1e9])
    client = StubBda(["InProgress"] * 10)
    with pytest.raises(ExtractionError, match="abandoned"):
        _live(client, StubS3(OUTPUT), timeout_seconds=1.0, clock=lambda: next(ticks)).extract(
            helios_fuel
        )


def test_the_output_location_is_content_addressed(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    """The same never-overwrite property `object_key` gives the evidence object.

    An extraction is a claim about specific bytes. Writing it to a path keyed only on the
    document id lets a re-extraction overwrite the output a lineage record was written
    against.
    """
    client = StubBda(["Success"])
    _live(client, StubS3(OUTPUT)).extract(helios_fuel)
    uri = client.invoked["outputConfiguration"]["s3Uri"]
    assert helios_fuel.content_sha256[:16] in uri
    assert helios_fuel.tenant in uri


def test_output_that_is_not_the_expected_shape_is_refused(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    """A parser that copes is repair logic, and repair logic under an extractor is how a
    misread page becomes a plausible number."""
    with pytest.raises(ExtractionError, match="expected shape"):
        parse_output({"metadata": {}}, helios_fuel)


def test_output_that_is_not_json_is_refused(repo_root: Path, helios_fuel: EvidenceDocument) -> None:
    with pytest.raises(ExtractionError, match="not JSON"):
        _live(StubBda(["Success"]), StubS3(b"<html>error</html>")).extract(helios_fuel)


def test_a_field_that_is_not_a_number_is_not_repaired(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    payload = {
        "metadata": {"number_of_pages": 1},
        "document": {"representation": {"text": ""}},
        "inference_result": {"net_amount_eur": {"value": "1 2З4,5O", "confidence": 0.4}},
    }
    extraction = parse_output(payload, helios_fuel)
    with pytest.raises(ExtractionError, match="not repaired"):
        extraction.require("net_amount_eur").as_decimal()


def test_a_missing_field_names_what_was_found(
    repo_root: Path, helios_fuel: EvidenceDocument
) -> None:
    extraction = parse_output(
        {
            "metadata": {"number_of_pages": 1},
            "document": {"representation": {"text": ""}},
            "inference_result": {"fuel_type": {"value": "diesel"}},
        },
        helios_fuel,
    )
    with pytest.raises(ExtractionError, match="fuel_type"):
        extraction.require("net_amount_eur")


# ── The injection scan still runs on what came back ──────────────────────────


def _ingest(repo_root: Path):
    """The ingest script, loaded as a module. It is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "attestor_ingest", repo_root / "pipelines" / "ingest" / "evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["attestor_ingest"] = module
    spec.loader.exec_module(module)
    return module


def test_the_scan_runs_on_extracted_text(repo_root: Path) -> None:
    """The poisoned attestation is read like any other document, and flagged on the text the
    extractor returned — not on a manifest field somebody set by hand."""
    module = _ingest(repo_root)

    report = module.IngestReport(tenant="helios")
    module.extract_all(
        repo_root,
        "helios",
        extractor=RecordedExtractor.from_root(repo_root),
        report=report,
    )
    assert any("INV-HEL-2026-0009" in note for note in report.flagged)
    assert report.rejected == []


def test_a_structured_export_without_a_capture_is_not_a_rejection(repo_root: Path) -> None:
    """Most of a corpus is files, not paper. Treating "nothing to read" as a failure would
    make the ingest red on a corpus that is entirely healthy."""
    module = _ingest(repo_root)

    report = module.IngestReport(tenant="lumen")
    found = module.extract_all(
        repo_root, "lumen", extractor=RecordedExtractor.from_root(repo_root), report=report
    )
    assert found == {}
    assert report.rejected == []


def test_a_document_whose_capture_is_stale_is_a_rejection(repo_root: Path) -> None:
    module = _ingest(repo_root)

    class AlwaysStale:
        def extract(self, document: EvidenceDocument) -> Extraction:
            raise StaleExtraction(f"{document.document_id} moved")

    report = module.IngestReport(tenant="helios")
    module.extract_all(repo_root, "helios", extractor=AlwaysStale(), report=report)
    assert report.rejected
    assert not report.ok


def test_the_period_a_document_covers_is_unchanged_by_extraction(repo_root: Path) -> None:
    """Extraction reads a document; it does not reinterpret what the document covers."""
    document = _document(repo_root, "helios", FUEL)
    assert document.covers(start=dt.date(2026, 1, 1), end=dt.date(2027, 1, 1))
