"""Per-tenant cost, metered where the charge happens.

`CostMeter` had no caller. It was a well-written module at zero percent coverage, which for a
repository whose own rules say "per-tenant cost telemetry is a first-class metric, not an
afterthought" and "done = runs + tested" is precisely an afterthought that was never run.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from attestor.agent.narrative import RecordedNarrativeProvider
from attestor.cli import main as cli
from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.datapoints.backends import QueryResult, RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.lineage import LineageLedger
from attestor.datapoints.resolver import ResolutionContext, ResolutionSet, Resolver
from attestor.observability import run_record
from attestor.observability.cost import CostMeter, Meter

TB = 1024**4


def _context(tenant: str = "helios", run_id: str = "run-01") -> ResolutionContext:
    return ResolutionContext(
        tenant=tenant,
        period="2026",
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2027, 1, 1),
        as_of=dt.date(2026, 7, 1),
        run_id=run_id,
    )


class Scanning(RecordedBackend):
    """A recorded backend that also reports what the scan would have cost."""

    # `**passthrough`, not a fixed signature. The resolver also hands the backend the
    # as-of pins now, and a stub that names every argument stops receiving the call the
    # moment one is added — silently, as an empty capture rather than a TypeError.
    def execute(self, *, sql, parameters, **passthrough):
        result = super().execute(sql=sql, parameters=parameters, **passthrough)
        return QueryResult(
            value=result.value,
            tables=result.tables,
            snapshot_ids=result.snapshot_ids,
            row_counts=result.row_counts,
            quarantined_rows=result.quarantined_rows,
            scanned_bytes=TB // 10,
        )


def _resolver(repo_root: Path, contract_set: ContractSet, meter: CostMeter) -> Resolver:
    return Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=Scanning(RecordedBackend.from_directory(repo_root / "recordings")._recordings),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        cost_meter=meter,
    )


# ── The meter is actually wired ──────────────────────────────────────────────


def test_a_resolution_attributes_its_scan_to_the_tenant(
    repo_root: Path, contract_set: ContractSet
) -> None:
    meter = CostMeter()
    _resolver(repo_root, contract_set, meter).resolve_all(_context())
    assert meter.charges, "no charge was recorded for a run that scanned a tenth of a terabyte"
    assert set(meter.by_tenant()) == {"helios"}
    assert meter.total > 0


def test_every_charge_names_the_session_that_incurred_it(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """Apportioning afterwards from a bill always flatters whichever tenant nobody watches."""
    meter = CostMeter()
    _resolver(repo_root, contract_set, meter).resolve_all(_context(run_id="run-77"))
    assert {charge.session_id for charge in meter.charges} == {"run-77"}
    assert {charge.meter for charge in meter.charges} == {Meter.ATHENA}


def test_a_resolver_without_a_meter_records_nothing(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """Metering is optional; the resolver does not depend on it."""
    resolver = Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=RecordedNarrativeProvider.from_root(repo_root),
    )
    assert resolver.resolve_all(_context()).can_issue


def test_a_zero_byte_scan_is_not_a_charge(repo_root: Path, contract_set: ContractSet) -> None:
    """A replayed query scans nothing, and a zero-euro line item is noise in a report."""
    meter = CostMeter()
    Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        cost_meter=meter,
    ).resolve_all(_context())
    assert meter.charges == []


def test_model_tokens_are_metered_from_the_provider(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """Token usage is read off the provider, not carried inside the draft an auditor reads."""
    provider = RecordedNarrativeProvider.from_root(repo_root)
    provider.last_usage = {"inputTokens": 4000, "outputTokens": 1000}

    meter = CostMeter()
    Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=provider,
        cost_meter=meter,
    ).resolve_all(_context())

    metered = {charge.meter for charge in meter.charges}
    assert Meter.MODEL_INPUT in metered
    assert Meter.MODEL_OUTPUT in metered


# ── The arithmetic ───────────────────────────────────────────────────────────


def test_a_charge_is_priced_from_the_table() -> None:
    meter = CostMeter()
    charge = meter.record(
        Meter.ATHENA, Decimal("2"), tenant="helios", session_id="s-1", operation="resolve"
    )
    assert charge.amount == Decimal("10.000000")


def test_per_report_needs_a_report() -> None:
    with pytest.raises(ValueError, match="at least one report"):
        CostMeter().per_report(0)


def test_the_report_separates_tenants_and_operations() -> None:
    meter = CostMeter()
    meter.record(Meter.ATHENA, 1, tenant="helios", session_id="s", operation="resolve")
    meter.record(Meter.RETRIEVAL, 40, tenant="aegis", session_id="s", operation="search")
    text = meter.report(reports=2)
    assert "helios" in text
    assert "aegis" in text
    assert "per report" in text
    assert set(meter.by_tenant()) == {"aegis", "helios"}


def _empty_results() -> ResolutionSet:
    """A resolution set with nothing in it. This file is about the meter, not the resolver."""
    return ResolutionSet({}, LineageLedger())


# ── Reaching the record ──────────────────────────────────────────────────────
#
# The meter was complete and unreachable. `Resolver` accepted `cost_meter: CostMeter | None`,
# `_meter` returned immediately when it was `None`, and the CLI never passed one — so every
# Athena scan and every model token was priced, attributed, and discarded. Every live run wrote
# `cost_eur = 0.0000` after querying a real lakehouse and drafting against a real model, and
# `attestor cost` printed "No estate is standing" with one standing.


def test_a_meters_totals_reach_the_run_record(repo_root, contract_set):
    """`€/report` and `€/tenant` are named as first-class metrics. Always-zero is not one."""

    meter = CostMeter()
    meter.record(
        Meter.MODEL_INPUT, 12, tenant="helios", session_id="s", operation="draft_narrative"
    )
    meter.record(
        Meter.ATHENA, "0.5", tenant="helios", session_id="s", operation="resolve_datapoint"
    )

    contracts = contract_set.for_standard(Standard.ESRS)
    record = run_record.build(
        run_id="test",
        tenant="helios",
        tenant_name="Helios",
        standard=Standard.ESRS.value,
        period="2026",
        started_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC),
        results=_empty_results(),
        contracts=contracts,
        cost_meter=meter,
    )

    assert Decimal(record.cost_eur) == meter.total
    assert Decimal(record.cost_eur) > 0
    assert set(record.cost_by_operation) == {"draft_narrative", "resolve_datapoint"}


def test_a_record_built_without_a_meter_says_zero_rather_than_guessing(repo_root, contract_set):

    record = run_record.build(
        run_id="test",
        tenant="helios",
        tenant_name="Helios",
        standard=Standard.ESRS.value,
        period="2026",
        started_at=dt.datetime(2026, 8, 8, tzinfo=dt.UTC),
        results=_empty_results(),
        contracts=contract_set.for_standard(Standard.ESRS),
    )
    assert Decimal(record.cost_eur) == 0


def test_a_fraction_of_a_cent_is_not_reported_as_nothing():
    """Four decimal places turned a real Athena scan into `0.0000`.

    A scan over a few megabytes is genuinely worth a fraction of a cent. Rounding it away
    reports a run that queried a lakehouse as having cost nothing, which is the sort of zero
    somebody eventually builds a decision on.
    """
    meter = CostMeter()
    meter.record(Meter.ATHENA, "0.000004", tenant="helios", session_id="s", operation="resolve")

    assert meter.total > 0
    assert f"{meter.total:.6f}" != "0.000000"
    assert f"{meter.total:.4f}" == "0.0000", "the reason six places are needed"


def test_the_cli_hands_the_resolver_a_meter(repo_root, monkeypatch):
    """The wiring, which is where this was broken and where nothing was looking.

    `Resolver` took `cost_meter: CostMeter | None`, `_meter` returned immediately when it was
    `None`, and `attestor run` never passed one. Both halves were correct in isolation and the
    seam between them threw every charge away. Testing the meter proved the meter; nothing
    proved that a report ever reached it.
    """

    seen: dict = {}

    def capture(tenant, root, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here; the call is what is under test")

    monkeypatch.setattr(cli, "_resolve", capture)
    with pytest.raises(RuntimeError):
        # `replay=None` explicitly: called directly rather than through typer, an unset option
        # is still an `OptionInfo` object, which is truthy and is not a path.
        cli.run_report(tenant="helios", root=repo_root, out=Path("out"), run_id="test", replay=None)

    assert isinstance(seen.get("cost_meter"), CostMeter), (
        "a report that does not meter itself reports EUR 0.0000 having queried a lakehouse"
    )


def test_a_resolver_built_without_one_meters_nothing(repo_root):
    """The default stays `None`, so an eval or a gate is not silently priced."""

    assert cli._resolver("helios", repo_root)._cost is None
    meter = CostMeter()
    assert cli._resolver("helios", repo_root, cost_meter=meter)._cost is meter
