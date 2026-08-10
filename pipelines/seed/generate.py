#!/usr/bin/env python3
"""Deterministic source data for the lake, built to reproduce the recorded answers exactly.

This is the hinge between the offline repository and a live estate, and it has one hard
requirement that is easy to state and easy to get subtly wrong:

    **A query run against this data must return the number in `recordings/`.**

If it does not, the offline suite and the deployed system are testing different programs.
Every gate, every eval and every claim in the README is asserted against the recordings; a
lake that produces *approximately* those figures makes all of them approximately true.

So the generator works backwards. It does not invent plausible activity and hope the totals
land nearby — it takes each recorded scalar as the target and constructs rows that sum to it
exactly, in `Decimal`, with the remainder placed on the final row rather than smeared. Then
`--check` re-runs the arithmetic the resolver's SQL performs and refuses to write anything if
a total is off by a cent.

The rows are synthetic and say so. What is *not* synthetic is the shape: the same columns,
the same `dq_status` vocabulary, the same quarantine behaviour, the same period boundaries.

Usage:
    python pipelines/seed/generate.py --out build/seed     # write Parquet locally
    python pipelines/seed/generate.py --check              # verify totals, write nothing
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import sys
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import yaml

ROOT = Path(__file__).resolve().parents[2]
PERIOD_START = dt.date(2026, 1, 1)
PERIOD_END = dt.date(2027, 1, 1)

#: Fixed. A generator seeded from the clock produces a lake that cannot be regenerated, and
#: "re-run it and see" stops being available exactly when somebody needs it.
SEED = 20260101


def _rng(*parts: str) -> random.Random:
    """A generator per stream, so adding one table does not shift the rows of another.

    `random` rather than `secrets` on purpose: this data must be *reproducible*, which is the
    opposite of what a cryptographic generator is for. Nothing here is a secret — every row
    is committed-adjacent synthetic activity whose totals are published in `recordings/`.
    """
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))  # noqa: S311 — reproducibility, not secrecy


def recorded(tenant: str) -> dict[str, Decimal | None]:
    """The scalars the offline suite asserts against, keyed by query path."""
    path = ROOT / "recordings" / f"{tenant}-2026.yaml"
    if not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        entry["query"]: (None if entry.get("value") is None else Decimal(str(entry["value"])))
        for entry in payload.get("results", [])
    }


def split(
    total: Decimal, count: int, *, rng: random.Random, places: str = "0.0001"
) -> list[Decimal]:
    """Split a total into `count` parts that sum to it **exactly**.

    The last part absorbs the rounding remainder rather than distributing it. Smearing the
    difference across every row would make each row slightly wrong and the total right, which
    is the failure mode that survives every spot check and none of the reconciliations.
    """
    quantum = Decimal(places)
    if count <= 0:
        raise ValueError("cannot split a total across no rows")
    weights = [Decimal(str(rng.uniform(0.6, 1.4))) for _ in range(count)]
    weight_total = sum(weights)
    parts = [
        (total * weight / weight_total).quantize(quantum, rounding=ROUND_HALF_EVEN)
        for weight in weights[:-1]
    ]
    parts.append(total - sum(parts))
    return parts


def dates(count: int, *, rng: random.Random) -> list[dt.date]:
    span = (PERIOD_END - PERIOD_START).days
    return sorted(PERIOD_START + dt.timedelta(days=rng.randrange(span)) for _ in range(count))


# ── Streams ──────────────────────────────────────────────────────────────────


def electricity(tenant: str, target: Decimal) -> list[dict[str, Any]]:
    """`SUM(kwh)/1000` must equal the recorded MWh, so the target is scaled up first."""
    rng = _rng(tenant, "electricity")
    rows_per_month = 4
    total_kwh = target * 1000
    amounts = split(total_kwh, 12 * rows_per_month, rng=rng)
    rows: list[dict[str, Any]] = []
    for index, kwh in enumerate(amounts):
        month = index // rows_per_month + 1
        day = min(1 + (index % rows_per_month) * 7, 28)
        rows.append(
            {
                "tenant_id": tenant,
                "site_id": f"SITE-{index % 5 + 1:02d}",
                "reading_date": dt.date(2026, month, day).isoformat(),
                "kwh": str(kwh),
                # Only actual readings. An estimate is real data about a bill and not a
                # measurement of consumption; the contract excludes them and so does this.
                "reading_type": "actual",
                "source_document_id": f"UTIL-{tenant[:3].upper()}-2026-{month:02d}",
                "dq_status": "clean",
                "ingested_at": "2027-01-05T00:00:00",
            }
        )
    # A handful of estimated rows, present so the resolver's `reading_type` filter is doing
    # visible work rather than being trivially true.
    for month in (3, 9):
        rows.append(
            {
                "tenant_id": tenant,
                "site_id": "SITE-06",
                "reading_date": dt.date(2026, month, 15).isoformat(),
                "kwh": "980.0000",
                "reading_type": "estimated",
                "source_document_id": f"UTIL-{tenant[:3].upper()}-2026-{month:02d}",
                "dq_status": "clean",
                "ingested_at": "2027-01-05T00:00:00",
            }
        )
    return rows


def scope_1(tenant: str, target: Decimal, *, quarantined: int = 0) -> list[dict[str, Any]]:
    rng = _rng(tenant, "scope1")
    count = 240
    rows = [
        {
            "tenant_id": tenant,
            "activity_date": day.isoformat(),
            "co2e_tonnes": str(amount),
            "consolidation_boundary": "operational_control",
            "dq_status": "clean",
        }
        for day, amount in zip(dates(count, rng=rng), split(target, count, rng=rng), strict=True)
    ]
    # Quarantined rows carry a boundary the contract does not recognise, which is why they
    # were rejected. They must not change the total, so they sit outside it.
    for _ in range(quarantined):
        rows.append(
            {
                "tenant_id": tenant,
                "activity_date": dt.date(2026, 6, 1).isoformat(),
                "co2e_tonnes": "12.0000",
                "consolidation_boundary": "unknown",
                "dq_status": "quarantined",
            }
        )
    return rows


def scope_3(tenant: str, target: Decimal, *, quarantined: int = 0) -> list[dict[str, Any]]:
    rng = _rng(tenant, "scope3")
    categories = ["cat_1_purchased_goods", "cat_4_upstream_transport", "cat_9_downstream_transport"]
    per_category = split(target, len(categories), rng=rng)
    rows: list[dict[str, Any]] = []
    for category, subtotal in zip(categories, per_category, strict=True):
        for day, amount in zip(dates(40, rng=rng), split(subtotal, 40, rng=rng), strict=True):
            rows.append(
                {
                    "tenant_id": tenant,
                    "activity_date": day.isoformat(),
                    "category": category,
                    "co2e_tonnes": str(amount),
                    "estimation_method": "supplier_specific",
                    "dq_status": "clean",
                }
            )
    for _ in range(quarantined):
        rows.append(
            {
                "tenant_id": tenant,
                "activity_date": dt.date(2026, 8, 1).isoformat(),
                "category": "cat_4_upstream_transport",
                "co2e_tonnes": "3.5000",
                "estimation_method": "spend_based",
                "dq_status": "quarantined",
            }
        )
    return rows


def ledger(tenant: str, target_meur: Decimal) -> list[dict[str, Any]]:
    """`SUM(amount_eur)/1e6` must equal the recorded MEUR."""
    rng = _rng(tenant, "ledger")
    count = 180
    amounts = split(target_meur * Decimal(1_000_000), count, rng=rng, places="0.01")
    return [
        {
            "tenant_id": tenant,
            "posting_date": day.isoformat(),
            "account_code": "4000",
            "amount_eur": str(amount),
            "period_status": "closed",
            "dq_status": "clean",
        }
        for day, amount in zip(dates(count, rng=rng), amounts, strict=True)
    ]


def scan_results(tenant: str) -> list[dict[str, Any]]:
    """One row per labelled passage, carrying what the scanner actually decided about it.

    Every other stream in this file is built backwards: a recording names the answer and the
    generator shapes rows that produce it. That is right for a lake standing in for an
    undertaking's ERP, where the point is to exercise the resolver's branches.

    It would be wrong here. These rows describe *this system's* robustness, and a set built to
    hit a chosen block rate would make the resulting disclosure a number about itself. So the
    real scanner runs over the real labelled corpus and the outcome is written down — which
    means a regression in the detector does not quietly reshape the seed, it fails
    `--check` and takes the Annex IV figures red with it.
    """
    from attestor.security import harness  # noqa: PLC0415 — only the lumen branch needs it

    score = harness.run(ROOT / "evals" / "injection" / "corpus.yaml")
    return [
        {
            "tenant_id": tenant,
            "assessed_at": "2026-06-30",
            "example_id": outcome.case.id,
            "corpus": "injection",
            "true_label": "manipulated" if outcome.case.poisoned else "benign",
            "predicted_label": "withheld" if outcome.detected else "admitted",
            "dq_status": "clean",
        }
        for outcome in sorted(score.outcomes, key=lambda outcome: outcome.case.id)
    ]


def evaluation(tenant: str, accuracy: Decimal, size: int) -> tuple[list[dict], list[dict]]:
    """Predictions whose accuracy is exactly the recorded ratio, and a matching matrix."""
    correct = int((accuracy * size).to_integral_value(rounding=ROUND_HALF_EVEN))
    predictions = [
        {
            "tenant_id": tenant,
            "evaluated_at": "2026-06-30",
            "example_id": f"EX-{index:06d}",
            "predicted_label": "conforming" if index < correct else "non_conforming",
            "true_label": "conforming",
            "is_held_out": True,
            "dq_status": "clean",
        }
        for index in range(size)
    ]
    confusion = [
        {
            "tenant_id": tenant,
            "evaluated_at": "2026-06-30",
            "predicted_label": "conforming",
            "true_label": "conforming",
            "count": correct,
            "dq_status": "clean",
        },
        {
            "tenant_id": tenant,
            "evaluated_at": "2026-06-30",
            "predicted_label": "non_conforming",
            "true_label": "conforming",
            "count": size - correct,
            "dq_status": "clean",
        },
    ]
    return predictions, confusion


# ── Assembly ─────────────────────────────────────────────────────────────────


def build(tenant: str) -> dict[str, list[dict[str, Any]]]:
    values = recorded(tenant)
    tables: dict[str, list[dict[str, Any]]] = {}

    if "esrs/e1_5_electricity_consumption.sql" in values:
        target = values["esrs/e1_5_electricity_consumption.sql"]
        tables["electricity_consumption"] = electricity(tenant, target)
        # The meter feed is an independent system, so it does not agree to the cent — it
        # agrees inside the contract's 1% bound, which is what the cross-check tests.
        meter_target = values["esrs/e1_5_electricity_consumption_crosscheck_meter.sql"]
        tables["meter_interval_reading"] = [
            {
                "tenant_id": tenant,
                "interval_start": f"2026-{month:02d}-01T00:00:00",
                "kwh": str(amount),
                "dq_status": "clean",
            }
            for month, amount in enumerate(
                split(meter_target * 1000, 12, rng=_rng(tenant, "meter")), start=1
            )
        ]

    if "esrs/e1_6_gross_scope_1.sql" in values:
        tables["ghg_scope_1_activity"] = scope_1(tenant, values["esrs/e1_6_gross_scope_1.sql"])
        spend_target = values["esrs/e1_6_gross_scope_1_crosscheck_fuel_spend.sql"]
        tables["procurement_fuel_spend"] = [
            {
                "tenant_id": tenant,
                "invoice_date": day.isoformat(),
                "fuel_type": "diesel",
                "net_amount_eur": str(amount),
                "dq_status": "clean",
            }
            for day, amount in zip(
                dates(60, rng=_rng(tenant, "spend")),
                split(spend_target * Decimal(1200), 60, rng=_rng(tenant, "spend"), places="0.01"),
                strict=True,
            )
        ]

    if "esrs/e1_6_gross_scope_3.sql" in values:
        quarantined = 1284 if tenant == "aegis" else 0
        tables["ghg_scope_3_activity"] = scope_3(
            tenant, values["esrs/e1_6_gross_scope_3.sql"], quarantined=quarantined
        )

    if "esrs/e1_6_net_revenue.sql" in values:
        target = values["esrs/e1_6_net_revenue.sql"]
        tables["general_ledger_posting"] = ledger(tenant, target)
        tables["financial_statement_extract"] = [
            {
                "tenant_id": tenant,
                "period_start": PERIOD_START.isoformat(),
                "period_end": PERIOD_END.isoformat(),
                # The filed figure and the ledger total are the same number, not two
                # estimates of it. ESRS E1-6 §55 requires exactly that.
                "net_revenue_eur": str(target * Decimal(1_000_000)),
                "statement_status": "filed",
                "dq_status": "clean",
            }
        ]

    if "ai_act/evaluation_accuracy.sql" in values:
        size = int(values["ai_act/evaluation_set_size.sql"])
        predictions, confusion = build_evaluation(tenant, values, size)
        tables["model_evaluation_prediction"] = predictions
        tables["model_evaluation_confusion"] = confusion
        tables["risk_register"] = [
            {
                "tenant_id": tenant,
                "assessed_at": "2026-06-30",
                "risk_id": f"RISK-{index:03d}",
                "mitigation_status": "complete",
                "residual_rating": "low",
                "dq_status": "clean",
            }
            for index in range(int(values["ai_act/open_residual_risks.sql"]))
        ] + [
            # Under mitigation, so it is not an accepted residual risk and must not be counted.
            {
                "tenant_id": tenant,
                "assessed_at": "2026-06-30",
                "risk_id": "RISK-900",
                "mitigation_status": "in_progress",
                "residual_rating": "high",
                "dq_status": "clean",
            }
        ]
        tables["incident_log"] = []
        tables["security_scan_result"] = scan_results(tenant)

    return tables


def build_evaluation(tenant: str, values: dict[str, Decimal | None], size: int):
    return evaluation(tenant, values["ai_act/evaluation_accuracy.sql"], size)


# ── Verification ─────────────────────────────────────────────────────────────


def verify(tenant: str, tables: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Re-run the arithmetic the resolver's SQL performs, and compare to the recording."""
    values = recorded(tenant)
    problems: list[str] = []

    def clean(rows: list[dict], **predicates) -> list[dict]:
        return [
            row
            for row in rows
            if row.get("dq_status") == "clean"
            and all(row.get(k) == v for k, v in predicates.items())
        ]

    def check(query: str, actual: Decimal) -> None:
        expected = values.get(query)
        if expected is None:
            return
        if actual != expected:
            problems.append(f"{query}: generated {actual}, recording says {expected}")

    if "electricity_consumption" in tables:
        rows = clean(tables["electricity_consumption"], reading_type="actual")
        check(
            "esrs/e1_5_electricity_consumption.sql",
            sum(Decimal(r["kwh"]) for r in rows) / 1000,
        )
    if "ghg_scope_1_activity" in tables:
        rows = clean(tables["ghg_scope_1_activity"], consolidation_boundary="operational_control")
        check("esrs/e1_6_gross_scope_1.sql", sum(Decimal(r["co2e_tonnes"]) for r in rows))
    if "ghg_scope_3_activity" in tables:
        rows = clean(tables["ghg_scope_3_activity"])
        check("esrs/e1_6_gross_scope_3.sql", sum(Decimal(r["co2e_tonnes"]) for r in rows))
    if "general_ledger_posting" in tables:
        rows = clean(tables["general_ledger_posting"], period_status="closed")
        check("esrs/e1_6_net_revenue.sql", sum(Decimal(r["amount_eur"]) for r in rows) / 1_000_000)
    if "model_evaluation_prediction" in tables:
        rows = clean(tables["model_evaluation_prediction"])
        correct = sum(1 for r in rows if r["predicted_label"] == r["true_label"])
        check(
            "ai_act/evaluation_accuracy.sql",
            (Decimal(correct) / Decimal(len(rows))).quantize(Decimal("0.0001")),
        )
        check("ai_act/evaluation_set_size.sql", Decimal(len({r["example_id"] for r in rows})))
    if "risk_register" in tables:
        rows = clean(tables["risk_register"], mitigation_status="complete")
        check("ai_act/open_residual_risks.sql", Decimal(len(rows)))
    if "incident_log" in tables:
        check("ai_act/serious_incidents.sql", Decimal(len(clean(tables["incident_log"]))))
    if "security_scan_result" in tables:
        rows = clean(tables["security_scan_result"], corpus="injection")
        for reason, truth in (("block_rate", "manipulated"), ("false_positive_rate", "benign")):
            labelled = [r for r in rows if r["true_label"] == truth]
            withheld = sum(1 for r in labelled if r["predicted_label"] == "withheld")
            check(
                f"ai_act/injection_{reason}.sql",
                (Decimal(withheld) / Decimal(len(labelled))).quantize(Decimal("0.0001")),
            )

    return problems


def write(tables: dict[str, list[dict[str, Any]]], *, tenant: str, out: Path) -> list[Path]:
    """Write newline-delimited JSON, one directory per table, partitioned by tenant.

    NDJSON rather than Parquet, and the reason is not laziness: Athena reads both, this
    repository has no Parquet dependency, and a seed anybody can open in a text editor is a
    seed anybody can check. The lake's *own* tables are Iceberg/Parquet — dbt writes those
    from this raw layer, which is exactly the shape a real ingestion has.
    """
    written: list[Path] = []
    for table, rows in sorted(tables.items()):
        directory = out / table / f"tenant_id={tenant}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "data.json"
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )
        written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "build" / "seed")
    parser.add_argument("--check", action="store_true", help="Verify totals; write nothing.")
    parser.add_argument("--tenant", help="One tenant, or all of them.")
    args = parser.parse_args()

    tenants = [args.tenant] if args.tenant else ["helios", "aegis", "lumen"]
    failures: list[str] = []

    for tenant in tenants:
        tables = build(tenant)
        if not tables:
            print(f"{tenant}: no recordings, nothing to generate")
            continue
        problems = verify(tenant, tables)
        failures.extend(f"{tenant}: {p}" for p in problems)
        rows = sum(len(v) for v in tables.values())
        status = "OK" if not problems else "MISMATCH"
        print(f"{tenant}: {len(tables)} table(s), {rows} row(s) — {status}")
        if not args.check and not problems:
            written = write(tables, tenant=tenant, out=args.out)
            print(f"  wrote {len(written)} file(s) under {args.out}")

    if failures:
        print(
            "\nthe generated lake does not reproduce the recorded answers. Offline and live "
            "would be testing different programs:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("\nevery generated total reproduces its recording exactly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
