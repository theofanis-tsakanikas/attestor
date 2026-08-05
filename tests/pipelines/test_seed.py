"""The hinge between the offline repository and a live estate.

Every gate, every eval and every claim in the README is asserted against `recordings/`. If a
query run against the seeded lake returns a different number, the offline suite and the
deployed system are testing different programs — and every one of those claims becomes
approximately true, which is the same as untrue.
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "attestor_seed", ROOT / "pipelines" / "seed" / "generate.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["attestor_seed"] = module
    spec.loader.exec_module(module)
    return module


seed = _module()


@pytest.mark.parametrize("tenant", ["helios", "aegis", "lumen"])
def test_the_lake_reproduces_every_recorded_answer(tenant: str) -> None:
    """The one test that keeps 'offline' and 'live' the same program."""
    problems = seed.verify(tenant, seed.build(tenant))
    assert not problems, problems


def test_generation_is_deterministic() -> None:
    """A lake seeded from the clock cannot be regenerated, which is when somebody needs it."""
    assert seed.build("helios") == seed.build("helios")


def test_a_split_sums_to_its_total_exactly() -> None:
    """The remainder lands on one row rather than being smeared across all of them.

    Smearing makes every row slightly wrong and the total right — the failure that survives
    every spot check and none of the reconciliations.
    """
    total = Decimal("18422.4118")
    parts = seed.split(total, 137, rng=seed._rng("t", "x"))
    assert sum(parts) == total
    assert len(parts) == 137


def test_quarantined_rows_do_not_change_a_total() -> None:
    """Aegis' Scope 3 is computed over incomplete data; the clean rows still sum to the record."""
    tables = seed.build("aegis")
    rows = tables["ghg_scope_3_activity"]
    assert any(row["dq_status"] == "quarantined" for row in rows)
    assert not seed.verify("aegis", tables)


def test_estimated_readings_exist_and_are_excluded() -> None:
    """Otherwise the contract's `reading_type` filter is trivially true and untested."""
    rows = seed.build("helios")["electricity_consumption"]
    assert any(row["reading_type"] == "estimated" for row in rows)
    assert not seed.verify("helios", seed.build("helios"))


def test_the_filed_figure_equals_the_ledger_total() -> None:
    """ESRS E1-6 §55: the intensity denominator is *the* reported net revenue, not a near one."""
    tables = seed.build("helios")
    ledger = sum(
        Decimal(row["amount_eur"])
        for row in tables["general_ledger_posting"]
        if row["period_status"] == "closed" and row["dq_status"] == "clean"
    )
    filed = Decimal(tables["financial_statement_extract"][0]["net_revenue_eur"])
    assert ledger == filed


def test_an_accuracy_no_evaluation_set_could_produce_is_caught() -> None:
    """The check that found a real defect: 0.9412 over 4,820 examples is not achievable."""
    size = 4820
    target = Decimal("0.9412")
    achievable = {
        (Decimal(correct) / Decimal(size)).quantize(Decimal("0.0001"))
        for correct in (int(target * size), int(target * size) + 1)
    }
    assert target not in achievable
