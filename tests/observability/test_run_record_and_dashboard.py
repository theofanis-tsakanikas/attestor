"""The results surface: what a run leaves behind, and how a human reads it."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.datapoints.backends import RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import NarrativeDraft, ResolutionContext, Resolver
from attestor.observability import dashboard, run_record
from attestor.observability.run_record import RunRecord

STARTED = dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC)


def _narrative(_contract, _context) -> NarrativeDraft:
    return NarrativeDraft(
        text="A plan exists. [ev:7f3a] It is funded. [ev:91c0] Minutes confirm. [ev:2d55]",
        citations=("ev:7f3a", "ev:91c0", "ev:2d55"),
        prompt_ref="p@1",
    )


def _resolve(repo_root: Path, contracts: ContractSet, tenant: str, evidence=None):
    resolver = Resolver(
        contracts=contracts,
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=evidence or EvidenceIndex.for_tenant(repo_root, tenant),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=_narrative,
    )
    return resolver.resolve_all(
        ResolutionContext(
            tenant=tenant,
            period="2026",
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2027, 1, 1),
            as_of=dt.date(2026, 7, 1),
        )
    )


def _record(
    repo_root: Path, contract_set: ContractSet, tenant: str, standard: Standard, evidence=None
):
    contracts = contract_set.for_standard(standard)
    results = _resolve(repo_root, contracts, tenant, evidence)
    return run_record.build(
        run_id="test",
        tenant=tenant,
        tenant_name=tenant.title(),
        standard=standard.value,
        period="2026",
        started_at=STARTED,
        results=results,
        contracts=contracts,
    )


@pytest.fixture
def issued(repo_root: Path, contract_set: ContractSet) -> RunRecord:
    return _record(repo_root, contract_set, "helios", Standard.ESRS)


@pytest.fixture
def blocked(repo_root: Path, contract_set: ContractSet) -> RunRecord:
    return _record(repo_root, contract_set, "aegis", Standard.ESRS)


# ── The record ───────────────────────────────────────────────────────────────


def test_an_issued_run_records_every_figure(issued: RunRecord) -> None:
    assert issued.issued
    assert issued.published
    assert all(entry.lineage_id for entry in issued.published)


def test_a_narrative_records_no_value(issued: RunRecord) -> None:
    narrative = next(e for e in issued.published if e.resolver_kind == "narrative")
    assert narrative.value is None


def test_a_blocked_run_still_produces_a_record(blocked: RunRecord) -> None:
    """The most useful output this system makes is 'we could not, and here is why'."""
    assert not blocked.issued
    assert blocked.blockers
    assert not blocked.artefacts


def test_figures_and_omissions_share_one_table(blocked: RunRecord) -> None:
    """Splitting them would make 'what did we not disclose' a join."""
    rows = blocked.datapoint_rows()
    assert {row["disclosed"] for row in rows} == {True, False}
    assert all("reason_code" in row for row in rows)


def test_the_run_row_carries_the_counts_a_trend_needs(issued: RunRecord) -> None:
    row = issued.run_row()
    assert row["published_count"] == len(issued.published)
    assert row["blocker_count"] == 0
    assert row["tenant_id"] == "helios"


def test_a_record_round_trips(issued: RunRecord, tmp_path: Path) -> None:
    issued.write(tmp_path)
    loaded = RunRecord.load_all(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].model_dump(mode="json") == issued.model_dump(mode="json")


def test_disclosure_rate_counts_everything_owed(blocked: RunRecord) -> None:
    assert 0.0 <= blocked.disclosure_rate < 1.0


# ── The page ─────────────────────────────────────────────────────────────────


def test_the_page_is_self_contained(issued: RunRecord) -> None:
    """It has to open from a folder after the estate is destroyed."""
    page = dashboard.render((issued,))
    for forbidden in ("<script", "http://", "https://", "src=", "@import"):
        assert forbidden not in page


def test_blockers_appear_before_figures(issued: RunRecord, blocked: RunRecord) -> None:
    page = dashboard.render((issued, blocked))
    assert page.index("Blockers") < page.index("Disclosed figures")


def test_a_blocked_tenant_is_marked_so(blocked: RunRecord) -> None:
    page = dashboard.render((blocked,))
    assert "cannot issue" in page
    assert blocked.blockers[0].reason_code in page


def test_an_expired_acceptance_is_shown_as_expired(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """A dashboard softer than the filing is worse than no dashboard."""
    thin = EvidenceIndex(
        [
            d
            for d in EvidenceIndex.for_tenant(repo_root, "helios")
            if d.document_class != "logistics_manifest"
        ],
        tenant="helios",
    )
    record = _record(repo_root, contract_set, "helios", Standard.ESRS, evidence=thin)
    assert record.limitations
    assert record.limitations[0].approvers

    page = dashboard.render((record,), today=dt.date(2027, 1, 5))
    assert "expired" in page
    assert "M. Andreadis" in page


def test_html_from_a_record_is_escaped() -> None:
    hostile = RunRecord(
        run_id="r",
        tenant="t",
        tenant_name="<script>alert(1)</script>",
        standard="ESRS",
        period="2026",
        started_at=STARTED,
    )
    page = dashboard.render((hostile,))
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_an_empty_dashboard_says_so() -> None:
    assert "No runs recorded yet" in dashboard.render(())


def test_an_observed_injection_reaches_the_record(repo_root: Path, contract_set: ContractSet):
    """Claim 1's reporting half, end to end.

    Detecting an attack and then discarding the detection is indistinguishable, from outside,
    from never detecting it. This walks the whole path — provider, resolver, record — because
    the defect it guards lived in the seam between two of them and each looked correct alone.
    """

    def reporting(_contract, _context) -> NarrativeDraft:
        return NarrativeDraft(
            text="A plan exists. [ev:7f3a] It is funded. [ev:91c0] Minutes confirm. [ev:2d55]",
            citations=("ev:7f3a", "ev:91c0", "ev:2d55"),
            prompt_ref="p@1",
            injection_observed=("INV-HEL-2026-0009: instructs the reader to restate Scope 1",),
        )

    contracts = contract_set.for_standard(Standard.ESRS)
    resolver = Resolver(
        contracts=contracts,
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=reporting,
    )
    results = resolver.resolve_all(
        ResolutionContext(
            tenant="helios",
            period="2026",
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2027, 1, 1),
            as_of=dt.date(2026, 7, 1),
        )
    )
    record = run_record.build(
        run_id="test",
        tenant="helios",
        tenant_name="Helios",
        standard=Standard.ESRS.value,
        period="2026",
        started_at=STARTED,
        results=results,
        contracts=contracts,
    )

    assert [f.observation for f in record.injection_findings] == [
        "INV-HEL-2026-0009: instructs the reader to restate Scope 1"
    ]
    # Reported, not blocking. The corpus is untrusted by construction, so an instruction found
    # inside it is the control working — a report that refused here would refuse on every
    # honest corpus that happens to quote an email.
    assert record.issued is True


def test_a_clean_run_reports_no_injection_findings(issued: RunRecord):
    assert issued.injection_findings == []
