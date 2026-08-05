#!/usr/bin/env python3
"""Generate the offline query recordings the test suite and evals replay.

Every recording carries a `provenance` field. Today they all say `synthetic`, because no
live run has happened yet. When the estate is stood up, the same script re-captures them
against Athena and stamps `live:<run-id>` — and `tests/datapoints/test_recordings.py`
asserts that anything claiming `live:` actually names a run.

The values below are deliberately shaped to exercise the resolver's branches rather than to
look plausible in isolation: `aegis` carries a Scope 1 cross-check that misses its tolerance,
because a repository where every fixture passes proves only that the happy path works.

Usage:
    python scripts/seed_recordings.py            # rewrite recordings/ from the current queries
    python scripts/seed_recordings.py --check    # fail if they are out of date (CI)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from attestor.datapoints.backends import query_digest

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / "recordings"

PERIOD = {"period_start": "2026-01-01", "period_end": "2027-01-01"}

# Per tenant: query path -> the recorded answer. `None` means "the query matched no rows",
# which the resolver treats as an evidence problem rather than as zero.
SCENARIOS: dict[str, dict[str, dict[str, Any]]] = {
    "helios": {
        "esrs/e1_6_gross_scope_1.sql": {
            "value": "18422.4118",
            "tables": ["gold.ghg_scope_1_activity"],
            "snapshot_ids": {"gold.ghg_scope_1_activity": "7284419023871123001"},
            "row_counts": {"gold.ghg_scope_1_activity": 214_883},
        },
        "esrs/e1_6_gross_scope_1_crosscheck_fuel_spend.sql": {
            # 0.34% off the primary — inside the contract's 0.5% bound.
            "value": "18485.0",
            "tables": ["gold.procurement_fuel_spend", "ref.fuel_price_period"],
            "snapshot_ids": {"gold.procurement_fuel_spend": "7284419023871123002"},
        },
        "esrs/e1_5_electricity_consumption.sql": {
            "value": "12904.6",
            "tables": ["gold.electricity_consumption"],
            "snapshot_ids": {"gold.electricity_consumption": "7284419023871123003"},
            "row_counts": {"gold.electricity_consumption": 1_448},
        },
        "esrs/e1_5_electricity_consumption_crosscheck_meter.sql": {
            "value": "12951.2",  # 0.36% — inside the 1% bound
            "tables": ["gold.meter_interval_reading"],
            "snapshot_ids": {"gold.meter_interval_reading": "7284419023871123004"},
        },
        "esrs/e1_6_gross_scope_3.sql": {
            "value": "204885.0",
            "tables": ["gold.ghg_scope_3_activity", "ref.scope_3_category_screening"],
            "snapshot_ids": {"gold.ghg_scope_3_activity": "7284419023871123005"},
        },
        "esrs/e1_6_net_revenue.sql": {
            "value": "486.22",
            "tables": ["gold.general_ledger_posting", "ref.chart_of_accounts"],
            "snapshot_ids": {"gold.general_ledger_posting": "7284419023871123006"},
        },
        "esrs/e1_6_net_revenue_crosscheck_ledger.sql": {
            "value": "486.22",  # the statutory figure agrees exactly, as it must
            "tables": ["gold.financial_statement_extract"],
            "snapshot_ids": {"gold.financial_statement_extract": "7284419023871123007"},
        },
    },
    # The AI Act tenant. Its documented system is Attestor itself, so these figures stand in
    # for this repository's own evaluation run until the estate captures a real one.
    "lumen": {
        "ai_act/evaluation_accuracy.sql": {
            "value": "0.9412",
            "tables": ["gold.model_evaluation_prediction"],
            "snapshot_ids": {"gold.model_evaluation_prediction": "3319419023871123001"},
            # 5,000 rather than a rounder-looking 4,820: 0.9412 x 4820 = 4536.584, and no
            # whole number of correct predictions over 4,820 examples rounds to 0.9412 at
            # four decimal places. A recorded accuracy that no evaluation set could actually
            # produce is a number that looks measured and was typed. `pipelines/seed` catches
            # exactly this, and it caught this.
            "row_counts": {"gold.model_evaluation_prediction": 5000},
        },
        "ai_act/evaluation_accuracy_crosscheck_confusion.sql": {
            # The same number at a different level of aggregation. It has to agree.
            "value": "0.9412",
            "tables": ["gold.model_evaluation_confusion"],
            "snapshot_ids": {"gold.model_evaluation_confusion": "3319419023871123002"},
        },
        "ai_act/evaluation_set_size.sql": {
            "value": "5000",
            "tables": ["gold.model_evaluation_prediction"],
            "snapshot_ids": {"gold.model_evaluation_prediction": "3319419023871123001"},
        },
        "ai_act/open_residual_risks.sql": {
            "value": "3",
            "tables": ["gold.risk_register"],
            "snapshot_ids": {"gold.risk_register": "3319419023871123003"},
        },
        "ai_act/serious_incidents.sql": {
            # Zero is an answer here, not an absence: COUNT over no rows is 0, and the
            # monitoring period genuinely contains no serious incident. An empty SUM would
            # have been a different thing entirely, which is why the resolver distinguishes
            # a NULL result from a zero one.
            "value": "0",
            "tables": ["gold.incident_log"],
            "snapshot_ids": {"gold.incident_log": "3319419023871123004"},
        },
    },
    "aegis": {
        "esrs/e1_6_gross_scope_1.sql": {
            "value": "9130.0",
            "tables": ["gold.ghg_scope_1_activity"],
            "snapshot_ids": {"gold.ghg_scope_1_activity": "5551419023871123001"},
        },
        "esrs/e1_6_gross_scope_1_crosscheck_fuel_spend.sql": {
            # 4.3% off. Two source systems disagree well beyond the 0.5% bound, so the figure
            # is refused: E_OUT_OF_TOLERANCE, the one internal failure a human may still
            # publish through, with dual approval and the discrepancy disclosed.
            "value": "9523.0",
            "tables": ["gold.procurement_fuel_spend", "ref.fuel_price_period"],
        },
        "esrs/e1_5_electricity_consumption.sql": {
            "value": "4417.9",
            "tables": ["gold.electricity_consumption"],
            "snapshot_ids": {"gold.electricity_consumption": "5551419023871123003"},
        },
        "esrs/e1_5_electricity_consumption_crosscheck_meter.sql": {
            "value": "4421.0",
            "tables": ["gold.meter_interval_reading"],
        },
        "esrs/e1_6_gross_scope_3.sql": {
            # Quarantined rows: the figure is computable but not from clean data.
            "value": "51204.0",
            "tables": ["gold.ghg_scope_3_activity"],
            "quarantined_rows": 1_284,
        },
        "esrs/e1_6_net_revenue.sql": {
            "value": "118.40",
            "tables": ["gold.general_ledger_posting", "ref.chart_of_accounts"],
        },
        "esrs/e1_6_net_revenue_crosscheck_ledger.sql": {
            "value": "118.40",
            "tables": ["gold.financial_statement_extract"],
        },
    },
}


def build(tenant: str, scenario: dict[str, dict[str, Any]]) -> dict[str, Any]:
    results = []
    for relative, recorded in scenario.items():
        sql = (ROOT / "queries" / relative).read_text(encoding="utf-8")
        results.append(
            {
                "query": relative,
                "query_digest": query_digest(sql),
                "parameters": {"tenant_id": tenant, **PERIOD},
                **recorded,
            }
        )
    return {
        "tenant": tenant,
        "period": "2026",
        "provenance": "synthetic",
        "note": (
            "Shaped by hand to exercise the resolver's branches. Re-captured against the "
            "live estate by `python scripts/seed_recordings.py --capture`, which stamps "
            "provenance with the run id."
        ),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if recordings are stale")
    args = parser.parse_args()

    RECORDINGS.mkdir(exist_ok=True)
    stale: list[str] = []
    for tenant, scenario in SCENARIOS.items():
        payload = build(tenant, scenario)
        rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        target = RECORDINGS / f"{tenant}-2026.yaml"
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                stale.append(str(target.relative_to(ROOT)))
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"wrote {target.relative_to(ROOT)}")

    if stale:
        print("stale recordings (a query changed without re-capture):", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
        print("run: python scripts/seed_recordings.py", file=sys.stderr)
        return 1
    if args.check:
        print("recordings are in sync with the queries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
