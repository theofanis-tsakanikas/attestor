#!/usr/bin/env python3
"""Every table the pipeline names exists, and every table the queries read has a producer.

Two halves of one failure, and both survived every offline check in this repository until an
estate was standing and `dbt build` ran for the first time.

**The sources were invented.** `schema.yml` declared `electricity_invoice`,
`fuel_transaction`, `general_ledger` and four more. Not one had ever existed; the raw tables
are `electricity_consumption`, `procurement_fuel_spend`, `general_ledger_posting`. `dbt parse`
resolves `source()` against `schema.yml` and not against a catalogue, so the project compiled
perfectly for as long as nobody pointed it at Athena.

**Ten gold tables had no producer.** `queries/` reads eleven; `pipelines/dbt` had five models.
The other ten were declared in Glue by `infra/data` and never written by anything, so every
resolver but one would have found an empty table — which is not an error, it is a figure of
zero, or an abstention for the wrong reason.

Neither is visible offline unless something compares the three descriptions of the same
lakehouse: what Terraform declares, what dbt reads, and what the queries select from. That
comparison is this file, and it needs no credentials to make.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DATA_LAYER = ROOT / "infra" / "data" / "main.tf"
DBT = ROOT / "pipelines" / "dbt"
QUERIES = ROOT / "queries"

#: `for_each` entries inside a named `aws_glue_catalog_table` resource.
RESOURCE = re.compile(
    r'resource "aws_glue_catalog_table" "(?P<label>\w+)" \{\n'
    r"  for_each = \{\n(?P<body>.*?)\n  \}\n",
    re.DOTALL,
)
ENTRY = re.compile(r"^    (\w+) = \[", re.MULTILINE)

#: `gold.<table>` in a resolver query. The `$snapshots` suffix is Iceberg metadata on the same
#: table, so it is stripped rather than treated as a table of its own.
GOLD_REFERENCE = re.compile(r'(?:gold|"gold")\.(?:"?)([a-z0-9_]+)(?:\$snapshots)?(?:"?)')


def terraform_tables() -> dict[str, set[str]]:
    text = DATA_LAYER.read_text(encoding="utf-8")
    return {m["label"]: set(ENTRY.findall(m["body"])) for m in RESOURCE.finditer(text)}


def dbt_sources() -> set[str]:
    schema = yaml.safe_load((DBT / "models" / "staging" / "schema.yml").read_text(encoding="utf-8"))
    return {
        table["name"] for source in schema.get("sources", []) for table in source.get("tables", [])
    }


def dbt_models(folder: str) -> set[str]:
    return {path.stem for path in (DBT / "models" / folder).glob("*.sql")}


def query_gold_tables() -> set[str]:
    names: set[str] = set()
    for path in QUERIES.rglob("*.sql"):
        text = re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))
        names.update(GOLD_REFERENCE.findall(text))
    return names


def main() -> int:
    problems: list[str] = []

    declared = terraform_tables()
    raw = declared.get("raw", set())
    terraform_gold = declared.get("gold", set())
    sources = dbt_sources()
    gold_models = dbt_models("gold")
    read_by_queries = query_gold_tables()

    if not raw or not sources or not read_by_queries:
        print(
            "  one of the three descriptions is empty; this check has lost a target",
            file=sys.stderr,
        )
        return 1

    for name in sorted(sources - raw):
        problems.append(
            f"dbt reads raw table `{name}`, which `infra/data` does not declare — "
            "`dbt parse` cannot see this, and the first live build fails on it"
        )
    for name in sorted(raw - sources):
        problems.append(
            f"`infra/data` declares raw table `{name}` and no dbt source reads it — either it "
            "is dead, or a staging model is missing"
        )

    producers = gold_models | terraform_gold
    for name in sorted(read_by_queries - producers):
        problems.append(
            f"a query reads `gold.{name}` and nothing produces it — no dbt model, no "
            "Terraform table. The resolver would find an empty table, which reads as a figure "
            "of zero rather than as a fault"
        )
    for name in sorted(gold_models & terraform_gold):
        problems.append(
            f"`gold.{name}` is declared by `infra/data` *and* materialised by dbt. Two owners "
            "for one table is how a schema ends up correct in Terraform and different in Athena"
        )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(
        f"  {len(raw)} raw, {len(producers)} gold producers, "
        f"{len(read_by_queries)} read by queries, {len(problems)} problem(s)"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
