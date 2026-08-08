"""The resolver, against the real contract set and the recorded backend.

Every test here runs the production resolution path. Nothing is stubbed except the boundary
to the lakehouse, and that boundary replays results captured against the same query text —
so a query edited without re-capture fails loudly rather than passing on a stale answer.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.contracts.overrides import Outcome
from attestor.datapoints import resolver as resolver_module
from attestor.datapoints.backends import RecordedBackend, StaleRecording
from attestor.datapoints.evidence import EvidenceDocument, EvidenceIndex
from attestor.datapoints.resolver import (
    Abstained,
    NarrativeDraft,
    ResolutionContext,
    Resolved,
    Resolver,
)

PERIOD_START = dt.date(2026, 1, 1)
PERIOD_END = dt.date(2027, 1, 1)
REPORT_DATE = dt.date(2026, 7, 1)
FIXED_NOW = dt.datetime(2026, 7, 1, 9, 0, tzinfo=dt.UTC)


def _context(tenant: str, as_of: dt.date = REPORT_DATE) -> ResolutionContext:
    return ResolutionContext(
        tenant=tenant,
        period="2026",
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        as_of=as_of,
        run_id="test",
    )


def _resolver(repo_root: Path, contract_set: ContractSet, tenant: str, **kwargs) -> Resolver:
    defaults = {
        # A resolver takes one standard at a time; both fixture tenants report ESRS.
        "contracts": contract_set.for_standard(Standard.ESRS),
        "backend": RecordedBackend.from_directory(repo_root / "recordings"),
        "evidence": EvidenceIndex.for_tenant(repo_root, tenant),
        "override_register": overrides.load_register(repo_root),
        "root": repo_root,
        "clock": lambda: FIXED_NOW,
    }
    return Resolver(**{**defaults, **kwargs})


def _complete_evidence(repo_root: Path, tenant: str = "helios") -> EvidenceIndex:
    """This tenant's corpus with the boundary gap filled in.

    `helios` is deliberately one logistics manifest short: its committed override accepts
    `E_PARTIAL_BOUNDARY` on Category 4, and the corpus tells that story so the acceptance is a
    real acceptance rather than a dormant entry. Tests about arithmetic — a total being the sum
    of its components, an intensity carrying a compound unit — are not about that gap, and
    borrowing a corpus shaped by somebody else's scenario is how they broke when it changed.
    """
    return EvidenceIndex(
        [
            *EvidenceIndex.for_tenant(repo_root, tenant),
            EvidenceDocument(
                document_id="LOGI-TEST-2026",
                tenant=tenant,
                document_class="logistics_manifest",
                covers_from=dt.date(2026, 1, 1),
                covers_to=dt.date(2026, 12, 31),
                content_sha256="0" * 64,
                source_uri="s3://test/logistics.parquet",
            ),
        ],
        tenant=tenant,
    )


def _narrative(_contract, _context) -> NarrativeDraft:
    return NarrativeDraft(
        text="The undertaking has a board-approved transition plan. [ev:7f3a]",
        citations=("ev:7f3a", "ev:91c0", "ev:2d55"),
        prompt_ref="esrs_e1_1_transition_plan@3",
    )


@pytest.fixture
def helios(repo_root: Path, contract_set: ContractSet) -> Resolver:
    """`helios` with its Category 4 boundary gap filled in.

    The committed corpus is one logistics manifest short on purpose, so the override that
    accepts `E_PARTIAL_BOUNDARY` is a real acceptance rather than a dormant entry. Most tests in
    this file are about arithmetic and lineage rather than about that gap, and the ones that are
    about it — `test_a_live_override_turns_a_block_into_a_declared_limitation` and its
    neighbour — build their own evidence anyway.
    """
    return _resolver(
        repo_root,
        contract_set,
        "helios",
        narrative_provider=_narrative,
        evidence=_complete_evidence(repo_root),
    )


@pytest.fixture
def aegis(repo_root: Path, contract_set: ContractSet) -> Resolver:
    return _resolver(repo_root, contract_set, "aegis", narrative_provider=_narrative)


# ── The happy path ───────────────────────────────────────────────────────────


def test_helios_can_issue(helios: Resolver) -> None:
    results = helios.resolve_all(_context("helios"))
    assert results.can_issue, [f"{a.datapoint_id}: {a.detail}" for a in results.blockers]


def test_scope_1_resolves_from_sql(helios: Resolver) -> None:
    result = helios.resolve_all(_context("helios"))["ESRS_E1-6_gross_scope_1"]
    assert isinstance(result, Resolved)
    assert result.value == Decimal("18422")  # rounded to the contract's precision of 0
    assert result.unit == "tCO2e"


def test_derived_scope_2_multiplies_energy_by_the_grid_factor(helios: Resolver) -> None:
    """12904.6 MWh x 0.3128 tCO2e/MWh = 4036.55888, half-even to 4037.

    Note the operands: the aggregate is built from the values *as disclosed*, not from the
    unrounded intermediates. That is what the contract's methodology says, and it is what
    lets a reader recompute the figure from the face of the statement.
    """
    result = helios.resolve_all(_context("helios"))["ESRS_E1-6_gross_scope_2_location"]
    assert isinstance(result, Resolved)
    assert result.value == Decimal("4037")
    assert result.unit == "tCO2e"


def test_total_is_the_sum_of_its_components(helios: Resolver) -> None:
    results = helios.resolve_all(_context("helios"))
    total = results["ESRS_E1-6_total_ghg"]
    parts = [
        results["ESRS_E1-6_gross_scope_1"],
        results["ESRS_E1-6_gross_scope_2_location"],
        results["ESRS_E1-6_gross_scope_3"],
    ]
    assert isinstance(total, Resolved)
    assert total.value == sum(p.value for p in parts)


def test_intensity_carries_a_compound_unit(helios: Resolver) -> None:
    result = helios.resolve_all(_context("helios"))["ESRS_E1-6_ghg_intensity"]
    assert isinstance(result, Resolved)
    assert result.unit == "tCO2e/MEUR"
    # 227343 tCO2e / 486.22 MEUR = 467.6
    assert result.value == Decimal("467.6")


def test_a_constant_records_who_approved_it(helios: Resolver) -> None:
    result = helios.resolve_all(_context("helios"))["ESRS_E1_grid_factor_GR"]
    assert isinstance(result, Resolved)
    assert "approved" in result.lineage.resolver_ref


def test_narrative_produces_prose_and_no_value(helios: Resolver) -> None:
    result = helios.resolve_all(_context("helios"))["ESRS_E1-1_transition_plan"]
    assert isinstance(result, Resolved)
    assert result.narrative is not None
    assert result.unit is None
    assert result.lineage.value is None  # a narrative lineage record carries no figure


# ── Refusals ─────────────────────────────────────────────────────────────────


def test_a_cross_check_disagreement_blocks_the_figure(aegis: Resolver) -> None:
    """Aegis' Scope 1 differs 4.3% between fuel volumes and procurement spend."""
    result = aegis.resolve_all(_context("aegis"))["ESRS_E1-6_gross_scope_1"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_OUT_OF_TOLERANCE"
    assert result.outcome is Outcome.BLOCKED


def test_quarantined_source_rows_block_the_figure(aegis: Resolver) -> None:
    result = aegis.resolve_all(_context("aegis"))["ESRS_E1-6_gross_scope_3"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_UPSTREAM_QUARANTINE"
    assert result.outcome is Outcome.BLOCKED


def test_aegis_cannot_issue(aegis: Resolver) -> None:
    results = aegis.resolve_all(_context("aegis"))
    assert not results.can_issue
    assert {b.datapoint_id for b in results.blockers} >= {
        "ESRS_E1-6_gross_scope_1",
        "ESRS_E1-6_gross_scope_3",
    }


def test_a_blocked_operand_propagates_to_its_aggregate(aegis: Resolver) -> None:
    """The total cannot be published when a component was refused. No partial sums."""
    results = aegis.resolve_all(_context("aegis"))
    total = results["ESRS_E1-6_total_ghg"]
    assert isinstance(total, Abstained)
    assert total.outcome is Outcome.BLOCKED


def test_missing_narrative_provider_does_not_invent_prose(
    repo_root: Path, contract_set: ContractSet
) -> None:
    resolver = _resolver(repo_root, contract_set, "helios", narrative_provider=None)
    result = resolver.resolve_all(_context("helios"))["ESRS_E1-1_transition_plan"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_METHOD_UNAVAILABLE"


def test_too_few_citations_refuses_the_narrative(
    repo_root: Path, contract_set: ContractSet
) -> None:
    def thin(_contract, _context) -> NarrativeDraft:
        return NarrativeDraft(text="Trust me.", citations=("ev:1",), prompt_ref="p@1")

    resolver = _resolver(repo_root, contract_set, "helios", narrative_provider=thin)
    result = resolver.resolve_all(_context("helios"))["ESRS_E1-1_transition_plan"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_NO_EVIDENCE"


def test_missing_evidence_abstains_before_any_query_runs(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """An empty corpus refuses at the evidence gate, not at the backend."""

    class Exploding:
        def execute(self, **_kwargs):  # pragma: no cover — must never be reached
            raise AssertionError("the backend was consulted despite missing evidence")

    resolver = _resolver(
        repo_root,
        contract_set,
        "helios",
        evidence=EvidenceIndex([], tenant="helios"),
        backend=Exploding(),
    )
    result = resolver.resolve_all(_context("helios"))["ESRS_E1-6_gross_scope_1"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_NO_EVIDENCE"


def test_a_backend_failure_becomes_the_unoverridable_reason(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """The catch-all exists so that an unforeseen crash can never become a number."""

    class Broken:
        def execute(self, **_kwargs):
            raise RuntimeError("connection reset")

    resolver = _resolver(repo_root, contract_set, "helios", backend=Broken())
    result = resolver.resolve_all(_context("helios"))["ESRS_E1-6_gross_scope_1"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_RESOLVER_ERROR"
    assert result.outcome is Outcome.BLOCKED
    with pytest.raises(overrides.NotOverridable):
        overrides.ensure_overridable(
            result.reason_code, overrides.OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION
        )


# ── Overrides in the resolution path ─────────────────────────────────────────


def test_a_live_override_turns_a_block_into_a_declared_limitation(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """The committed helios override covers Scope 3's boundary gap for 2026."""

    class PartialBoundary(RecordedBackend):
        pass

    resolver = _resolver(
        repo_root,
        contract_set,
        "helios",
        evidence=EvidenceIndex(
            [
                d
                for d in EvidenceIndex.for_tenant(repo_root, "helios")
                if d.document_class != "logistics_manifest"
            ],
            tenant="helios",
        ),
        narrative_provider=_narrative,
    )
    result = resolver.resolve_all(_context("helios"))["ESRS_E1-6_gross_scope_3"]
    assert isinstance(result, Abstained)
    assert result.reason_code == "E_PARTIAL_BOUNDARY"
    assert result.outcome is Outcome.OMITTED_WITH_MATERIAL_LIMITATION
    assert result.override is not None


def test_the_same_gap_blocks_once_the_override_has_lapsed(
    repo_root: Path, contract_set: ContractSet
) -> None:
    resolver = _resolver(
        repo_root,
        contract_set,
        "helios",
        evidence=EvidenceIndex(
            [
                d
                for d in EvidenceIndex.for_tenant(repo_root, "helios")
                if d.document_class != "logistics_manifest"
            ],
            tenant="helios",
        ),
        narrative_provider=_narrative,
    )
    result = resolver.resolve_all(_context("helios", as_of=dt.date(2027, 1, 5)))[
        "ESRS_E1-6_gross_scope_3"
    ]
    assert isinstance(result, Abstained)
    assert result.outcome is Outcome.BLOCKED


# ── Scoping ──────────────────────────────────────────────────────────────────


def test_a_resolver_refuses_to_resolve_a_tenant_it_is_not_scoped_to(helios: Resolver) -> None:
    with pytest.raises(ValueError, match="scoped to tenant"):
        helios.resolve_all(_context("aegis"))


def test_the_tenant_reaches_the_query_as_a_bound_parameter(helios: Resolver) -> None:
    record = helios.resolve_all(_context("helios")).ledger["ESRS_E1-6_gross_scope_1"]
    assert record.parameters["tenant_id"] == "helios"


# ── Reproducibility (claim 4) ────────────────────────────────────────────────


def test_two_runs_over_the_same_data_agree_on_every_lineage_id(helios: Resolver) -> None:
    first = resolver_module.summarise(helios.resolve_all(_context("helios")))
    second = resolver_module.summarise(helios.resolve_all(_context("helios")))
    assert first == second


def test_the_clock_is_outside_the_lineage_hash(repo_root: Path, contract_set: ContractSet) -> None:
    """A figure resolved twice from the same data is the same figure."""
    later = dt.datetime(2027, 3, 3, 17, 30, tzinfo=dt.UTC)
    now_run = _resolver(repo_root, contract_set, "helios", narrative_provider=_narrative)
    later_run = _resolver(
        repo_root, contract_set, "helios", narrative_provider=_narrative, clock=lambda: later
    )
    assert (
        now_run.resolve_all(_context("helios")).ledger.ids()
        == later_run.resolve_all(_context("helios")).ledger.ids()
    )


def test_different_tenants_produce_different_lineage(helios: Resolver, aegis: Resolver) -> None:
    a = helios.resolve_all(_context("helios")).ledger["ESRS_E1-6_gross_scope_1"].lineage_id
    b = aegis.resolve_all(_context("aegis")).ledger.get("ESRS_E1-6_gross_scope_1")
    assert b is None or a != b.lineage_id


def test_a_derived_figure_records_its_operand_lineage(helios: Resolver) -> None:
    ledger = helios.resolve_all(_context("helios")).ledger
    derived = ledger["ESRS_E1-6_gross_scope_2_location"]
    assert set(derived.inputs) == {
        ledger["ESRS_E1-5_electricity_consumption"].lineage_id,
        ledger["ESRS_E1_grid_factor_GR"].lineage_id,
    }


def test_the_annex_names_the_snapshot_each_figure_was_read_from(helios: Resolver) -> None:
    annex = helios.resolve_all(_context("helios")).ledger.as_annex()
    scope_1 = next(row for row in annex if row["datapoint"] == "ESRS_E1-6_gross_scope_1")
    assert scope_1["sources"] == ["gold.ghg_scope_1_activity@7284419023871123001"]


# ── Recordings stay honest ───────────────────────────────────────────────────


def test_editing_a_query_without_recapturing_fails_loudly(repo_root: Path) -> None:
    backend = RecordedBackend.from_directory(repo_root / "recordings")
    with pytest.raises(StaleRecording, match="Re-capture it"):
        backend.execute(
            sql="SELECT SUM(x) FROM somewhere WHERE tenant_id = :tenant_id -- edited",
            parameters={
                "tenant_id": "helios",
                "period_start": "2026-01-01",
                "period_end": "2027-01-01",
            },
            snapshot_id=None,
        )
