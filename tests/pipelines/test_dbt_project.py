"""The dbt project must parse. Every failure here is discovered after the estate is up."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DBT = Path(__file__).resolve().parents[2] / "pipelines" / "dbt"


def _project() -> dict:
    return yaml.safe_load((DBT / "dbt_project.yml").read_text(encoding="utf-8"))


def test_every_package_a_test_uses_is_declared() -> None:
    """`dbt_utils.accepted_range` with no packages.yml fails at parse, after the apply."""
    schema = (DBT / "models" / "staging" / "schema.yml").read_text(encoding="utf-8")
    used = set(re.findall(r"(\w+)\.\w+:", schema)) & {"dbt_utils"}
    declared = {
        entry["package"].split("/")[-1].replace("-", "_")
        for entry in yaml.safe_load((DBT / "packages.yml").read_text(encoding="utf-8"))["packages"]
    }
    assert used <= declared, f"used {used}, declared {declared}"


def test_every_ref_resolves_to_a_model_or_a_seed() -> None:
    """A `relationships` test pointing at a model nobody wrote fails the whole run."""
    models = {p.stem for p in (DBT / "models").rglob("*.sql")}
    seeds = {p.stem for p in (DBT / "seeds").glob("*.csv")}
    known = models | seeds

    referenced: set[str] = set()
    for path in list((DBT / "models").rglob("*.yml")) + list((DBT / "models").rglob("*.sql")):
        referenced |= set(re.findall(r"ref\(['\"]([a-z0-9_]+)['\"]\)", path.read_text()))

    assert referenced <= known, f"dangling refs: {sorted(referenced - known)}"


def test_seed_paths_are_configured() -> None:
    assert "seeds" in _project().get("seed-paths", [])


def test_the_quarantine_view_is_built_after_every_run() -> None:
    """`store_failures` writes one table per test; the resolver wants one relation."""
    hooks = " ".join(_project().get("on-run-end", []))
    assert "build_quarantine_view" in hooks


def test_nothing_reads_a_schema_that_is_never_created() -> None:
    """`<schema>_quarantine.all_failures` was read by two files and created by none."""
    for path in list((DBT / "models").rglob("*.sql")) + list((DBT / "macros").glob("*.sql")):
        body = path.read_text(encoding="utf-8")
        assert "_quarantine.all_failures" not in body, path.name
