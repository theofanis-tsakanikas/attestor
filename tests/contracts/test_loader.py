"""Referential integrity across the whole contract set.

The failure this prevents is the quiet one: a query is renamed, a contract still points at
the old path, and at runtime the resolver falls back to *something*. There is no fallback.
A dangling reference fails the build, on a laptop, in under a second.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from attestor.contracts import derivation, loader
from attestor.contracts.loader import ContractSet, ContractSetError
from attestor.contracts.model import Standard


def test_the_real_contract_set_loads(contract_set: ContractSet) -> None:
    """If this fails, the repository is inconsistent and nothing downstream is meaningful."""
    assert len(contract_set) > 0


def test_every_contract_has_a_source_file(contract_set: ContractSet) -> None:
    for datapoint_id in contract_set.contracts:
        assert datapoint_id in contract_set.sources


def test_esrs_contracts_are_present(contract_set: ContractSet) -> None:
    esrs = contract_set.for_standard(Standard.ESRS)
    assert {c.id for c in esrs} >= {
        "ESRS_E1-6_gross_scope_1",
        "ESRS_E1-6_gross_scope_2_location",
        "ESRS_E1-6_total_ghg",
    }


def test_resolution_order_places_operands_first(contract_set: ContractSet) -> None:
    order = contract_set.resolution_order()
    position = {datapoint_id: index for index, datapoint_id in enumerate(order)}
    assert set(order) == set(contract_set.contracts)

    for contract in contract_set:
        if contract.resolver.kind != "derived":
            continue
        for operand in derivation.referenced_datapoints(contract.resolver.expression):
            assert position[operand] < position[contract.id], (
                f"{operand} must resolve before {contract.id}"
            )


def test_derived_dimensions_agree_with_declared_units(contract_set: ContractSet) -> None:
    """Already enforced by the loader; asserted here so the guarantee is visible in the suite."""
    dimensions = contract_set.dimensions
    for contract in contract_set:
        if contract.resolver.kind != "derived":
            continue
        inferred = derivation.infer_dimension(contract.resolver.expression, dict(dimensions))
        assert inferred == contract.dimension, contract.id


def test_no_figure_is_derived_from_generated_prose(contract_set: ContractSet) -> None:
    for contract in contract_set:
        if contract.resolver.kind != "derived":
            continue
        for operand in derivation.referenced_datapoints(contract.resolver.expression):
            assert not contract_set[operand].is_model_authored


def test_every_sql_resolver_points_at_a_file_that_exists(
    contract_set: ContractSet, repo_root: Path
) -> None:
    for contract in contract_set:
        if contract.resolver.kind == "sql":
            assert (repo_root / "queries" / contract.resolver.query).is_file(), contract.id


def test_every_narrative_resolver_points_at_a_prompt_that_exists(
    contract_set: ContractSet, repo_root: Path
) -> None:
    for contract in contract_set:
        if contract.resolver.kind == "narrative":
            prompt = repo_root / "prompts" / f"{contract.resolver.prompt_id}.md"
            assert prompt.is_file(), contract.id


def test_every_cross_check_query_exists(contract_set: ContractSet, repo_root: Path) -> None:
    for contract in contract_set:
        for query in contract.tolerance.cross_check:
            assert (repo_root / "queries" / query).is_file(), f"{contract.id} → {query}"


def test_queries_bind_parameters_rather_than_interpolating(repo_root: Path) -> None:
    """A query that builds its own tenant predicate by concatenation is a leak waiting to happen."""
    for query in (repo_root / "queries").rglob("*.sql"):
        text = query.read_text(encoding="utf-8")
        assert ":tenant_id" in text, f"{query.name} does not scope by a bound tenant parameter"
        for forbidden in ("' +", "+ '", "format(", "%s"):
            assert forbidden not in text, f"{query.name} looks like it interpolates: {forbidden!r}"


# ── The loader catching real problems ────────────────────────────────────────


@pytest.fixture
def scratch_repo(tmp_path: Path, repo_root: Path) -> Path:
    """A throwaway repository holding one valid contract, ready to be broken."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='scratch'\n", encoding="utf-8")
    (tmp_path / "contracts" / "esrs").mkdir(parents=True)
    (tmp_path / "queries" / "esrs").mkdir(parents=True)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "queries" / "esrs" / "base.sql").write_text(
        "SELECT 1 AS value WHERE tenant_id = :tenant_id", encoding="utf-8"
    )
    _write(
        tmp_path / "contracts" / "esrs" / "base.yaml",
        {
            "id": "ESRS_BASE_energy",
            "standard": "ESRS",
            "standard_version": "2023-12",
            "reference": "ESRS TEST §1",
            "title": "Base energy",
            "kind": "quantitative",
            "unit": "MWh",
            "precision": 1,
            "resolver": {"kind": "sql", "query": "esrs/base.sql"},
            "evidence": {"required": True, "classes": ["utility_invoice"]},
        },
    )
    return tmp_path


def _write(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_scratch_repo_is_valid_before_being_broken(scratch_repo: Path) -> None:
    """Every mutation test below is meaningless unless the starting point is green."""
    assert len(loader.load(scratch_repo)) == 1


def test_dangling_query_reference_fails(scratch_repo: Path) -> None:
    _write(
        scratch_repo / "contracts" / "esrs" / "dangling.yaml",
        {
            "id": "ESRS_DANGLING_thing",
            "standard": "ESRS",
            "standard_version": "2023-12",
            "reference": "ESRS TEST §2",
            "title": "Dangling",
            "kind": "quantitative",
            "unit": "MWh",
            "precision": 1,
            "resolver": {"kind": "sql", "query": "esrs/renamed_last_week.sql"},
            "evidence": {"required": True, "classes": ["utility_invoice"]},
        },
    )
    with pytest.raises(ContractSetError, match="does not exist"):
        loader.load(scratch_repo)


def test_duplicate_datapoint_id_fails(scratch_repo: Path) -> None:
    _write(
        scratch_repo / "contracts" / "esrs" / "copy.yaml",
        yaml.safe_load((scratch_repo / "contracts" / "esrs" / "base.yaml").read_text()),
    )
    with pytest.raises(ContractSetError, match="duplicate datapoint id"):
        loader.load(scratch_repo)


def test_derived_dimension_mismatch_fails(scratch_repo: Path) -> None:
    """Declaring tCO2e for something that produces MWh does not load."""
    _write(
        scratch_repo / "contracts" / "esrs" / "wrong_unit.yaml",
        {
            "id": "ESRS_WRONG_unit",
            "standard": "ESRS",
            "standard_version": "2023-12",
            "reference": "ESRS TEST §3",
            "title": "Wrongly declared",
            "kind": "quantitative",
            "unit": "tCO2e",
            "precision": 0,
            "boundary": "operational_control",
            "resolver": {"kind": "derived", "expression": "{ESRS_BASE_energy} * 2"},
            "evidence": {"required": False, "inherited": True},
        },
    )
    with pytest.raises(ContractSetError, match="but the expression produces"):
        loader.load(scratch_repo)


def test_derivation_cycle_is_named(scratch_repo: Path) -> None:
    for name, expression in (("A", "{ESRS_CYCLE_b}"), ("B", "{ESRS_CYCLE_a}")):
        _write(
            scratch_repo / "contracts" / "esrs" / f"cycle_{name.lower()}.yaml",
            {
                "id": f"ESRS_CYCLE_{name.lower()}",
                "standard": "ESRS",
                "standard_version": "2023-12",
                "reference": f"ESRS TEST §{name}",
                "title": f"Cycle {name}",
                "kind": "quantitative",
                "unit": "MWh",
                "precision": 1,
                "resolver": {"kind": "derived", "expression": expression},
                "evidence": {"required": False, "inherited": True},
            },
        )
    with pytest.raises(ContractSetError, match="derivation cycle"):
        loader.load(scratch_repo)


def test_reference_to_an_undefined_datapoint_fails(scratch_repo: Path) -> None:
    _write(
        scratch_repo / "contracts" / "esrs" / "undefined.yaml",
        {
            "id": "ESRS_UNDEFINED_operand",
            "standard": "ESRS",
            "standard_version": "2023-12",
            "reference": "ESRS TEST §4",
            "title": "Undefined operand",
            "kind": "quantitative",
            "unit": "MWh",
            "precision": 1,
            "resolver": {"kind": "derived", "expression": "{ESRS_NOT_here} * 2"},
            "evidence": {"required": False, "inherited": True},
        },
    )
    with pytest.raises(ContractSetError, match="undefined datapoint"):
        loader.load(scratch_repo)


def test_validate_reports_without_raising(scratch_repo: Path) -> None:
    assert loader.validate(scratch_repo) == []
    (scratch_repo / "contracts" / "esrs" / "broken.yaml").write_text(
        "id: not-an-id\n", encoding="utf-8"
    )
    issues = loader.validate(scratch_repo)
    assert issues and "broken.yaml" in str(issues[0])
