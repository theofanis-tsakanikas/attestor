#!/usr/bin/env python3
"""Ask the standing estate whether it did what it claims, and answer from the estate.

Not a substitute for the offline suite — that one proves the logic, on a laptop, for free.
This proves the *deployment*: that the thing running in an account behaves the way the thing
in the repository does. Those come apart quietly. Every failure in this deploy was of exactly
that shape — a config key AWS ignores, a filter with nothing to match on, a job that reports
success while indexing nothing — and none of them was visible from a green test run.

Needs credentials, so it lives outside preflight and is invoked by hand or by the deploy.
Every check reads from the account or from the run records the account produced; nothing here
asserts against a fixture.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: What each tenant is *supposed* to do. `aegis` failing is the point of `aegis`: its Scope 1
#: misses its own cross-check by more than tolerance, and a run that issued it anyway would
#: mean the tolerance was decorative.
EXPECTED = {
    "helios": {"issued": True, "reasons": set()},
    "aegis": {"issued": False, "reasons": {"E_OUT_OF_TOLERANCE", "E_UPSTREAM_QUARANTINE"}},
    "lumen": {"issued": True, "reasons": set()},
}


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append(Result(name, ok, detail))

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.ok]


#: Resolved once, so the probe cannot be pointed at a different `aws` by a changed PATH
#: between one check and the next.
AWS = shutil.which("aws") or "aws"


def aws(*args: str) -> str:
    """One AWS CLI call. Raises on failure so a broken probe cannot read as a pass."""
    return subprocess.run(  # noqa: S603
        [AWS, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def run_records(directory: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        tenant = payload.get("tenant") or payload.get("tenant_id")
        if tenant:
            records[str(tenant)] = payload
    return records


def check_outcomes(report: Report, records: dict[str, dict]) -> None:
    """Each tenant did what it is supposed to do — including the one that must refuse."""
    for tenant, expected in EXPECTED.items():
        record = records.get(tenant)
        if record is None:
            report.check(f"{tenant}: run record", False, "no record produced")
            continue

        issued = bool(record.get("issued"))
        report.check(
            f"{tenant}: {'issues' if expected['issued'] else 'refuses'}",
            issued == expected["issued"],
            f"issued={issued}, expected={expected['issued']}",
        )

        reasons = {
            str(d.get("reason_code")) for d in record.get("datapoints", []) if d.get("reason_code")
        }
        missing = expected["reasons"] - reasons
        report.check(
            f"{tenant}: refuses for the documented reasons",
            not missing,
            f"missing {sorted(missing)}" if missing else f"saw {sorted(reasons)}",
        )

        # Claim 3, on the artefacts this run actually produced: a disclosed figure carries a
        # lineage id, because a figure with no lineage is a figure nobody can re-derive.
        unlineaged = [
            d.get("datapoint_id")
            for d in record.get("datapoints", [])
            if d.get("disclosed") and not d.get("lineage_id")
        ]
        report.check(
            f"{tenant}: every disclosed figure carries lineage",
            not unlineaged,
            f"missing on {unlineaged}" if unlineaged else "",
        )


def check_isolation(report: Report, evidence_kb: str) -> None:
    """Claim 2, against the live index rather than against a mock.

    The filter is applied by Bedrock at the index. This asks each tenant's filter for the
    other tenant's most distinctive document and requires nothing to come back.
    """
    probes = {"helios": "Attestor human oversight procedure", "lumen": "fleet transition plan"}
    foreign = {"helios": "lumen", "lumen": "helios"}
    for tenant, query in probes.items():
        raw = aws(
            "bedrock-agent-runtime",
            "retrieve",
            "--knowledge-base-id",
            evidence_kb,
            "--retrieval-query",
            json.dumps({"text": query}),
            "--retrieval-configuration",
            json.dumps(
                {
                    "vectorSearchConfiguration": {
                        "numberOfResults": 10,
                        "filter": {"equals": {"key": "tenant", "value": tenant}},
                    }
                }
            ),
            "--query",
            "retrievalResults[].location.s3Location.uri",
            "--output",
            "json",
        )
        uris = json.loads(raw or "[]")
        leaked = [u for u in uris if f"/{foreign[tenant]}/" in u]
        report.check(
            f"isolation: {tenant}'s filter returns no {foreign[tenant]} document",
            not leaked,
            f"leaked {leaked}" if leaked else f"{len(uris)} own-tenant passage(s)",
        )


def check_gold_is_iceberg(report: Report, database: str) -> None:
    """Claim 4 rests on snapshots, and only an Iceberg table has them."""
    raw = aws(
        "glue",
        "get-tables",
        "--database-name",
        database,
        "--query",
        "TableList[].{n:Name,t:Parameters.table_type}",
        "--output",
        "json",
    )
    tables = json.loads(raw or "[]")
    hive = sorted(t["n"] for t in tables if (t.get("t") or "").upper() != "ICEBERG")
    report.check(
        "every gold table is Iceberg",
        not hive,
        f"not Iceberg: {hive}" if hive else f"{len(tables)} table(s)",
    )


def check_injection_surfaced(report: Report, records: dict[str, dict]) -> None:
    """Claim 1's *reporting* half: the poisoned document is a finding, not a silent drop."""
    manifest = (ROOT / "evidence" / "helios" / "2026.yaml").read_text(encoding="utf-8")
    report.check(
        "the poisoned document is still in the corpus",
        "INV-HEL-2026-0009" in manifest,
        "a control with nothing to catch proves nothing",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", default="out/runs", help="directory of run records")
    parser.add_argument("--evidence-kb", required=True)
    parser.add_argument("--database", default="attestor_gold")
    arguments = parser.parse_args()

    report = Report()
    records = run_records(ROOT / arguments.runs)
    if not records:
        print(f"  no run records under {arguments.runs}", file=sys.stderr)
        return 1

    check_outcomes(report, records)
    check_isolation(report, arguments.evidence_kb)
    check_gold_is_iceberg(report, arguments.database)
    check_injection_surfaced(report, records)

    for result in report.results:
        mark = "ok  " if result.ok else "FAIL"
        print(f"  {mark} {result.name}" + (f"  — {result.detail}" if result.detail else ""))
    print(f"\n  {len(report.results) - len(report.failed)}/{len(report.results)} passed")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
