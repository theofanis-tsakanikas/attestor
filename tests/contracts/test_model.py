"""Contract invariants — the boundary between the model and the numbers, as type errors."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from attestor.contracts.model import DatapointContract


def _build(base: dict, **overrides) -> DatapointContract:
    return DatapointContract.model_validate({**base, **overrides})


def test_minimal_contract_validates(contract_yaml: dict) -> None:
    contract = _build(contract_yaml)
    assert contract.is_quantitative
    assert not contract.is_model_authored


# ── The boundary ─────────────────────────────────────────────────────────────


def test_narrative_resolver_cannot_serve_a_quantitative_datapoint(contract_yaml: dict) -> None:
    """The single most important invariant in the repository."""
    with pytest.raises(ValidationError) as excinfo:
        _build(
            contract_yaml,
            resolver={"kind": "narrative", "prompt_id": "esrs_e1_1_transition_plan"},
        )
    assert "must never become a figure" in str(excinfo.value)


def test_narrative_datapoint_cannot_use_a_sql_resolver(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _build(contract_yaml, kind="narrative", unit=None, precision=None)
    assert "must use a narrative resolver" in str(excinfo.value)


def test_narrative_datapoint_validates_with_a_narrative_resolver(contract_yaml: dict) -> None:
    contract = _build(
        contract_yaml,
        kind="narrative",
        unit=None,
        precision=None,
        resolver={"kind": "narrative", "prompt_id": "esrs_e1_1_transition_plan"},
    )
    assert contract.is_model_authored


# ── Quantitative completeness ────────────────────────────────────────────────


def test_quantitative_datapoint_needs_a_unit(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="must declare a unit"):
        _build(contract_yaml, unit=None)


def test_quantitative_datapoint_needs_precision(contract_yaml: dict) -> None:
    """Rounding is a disclosure decision, not a formatting preference."""
    with pytest.raises(ValidationError, match="must declare precision"):
        _build(contract_yaml, precision=None)


def test_unknown_unit_is_rejected(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="unknown unit"):
        _build(contract_yaml, unit="furlongs")


def test_non_quantitative_datapoint_carries_no_unit(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="only quantitative datapoints carry a unit"):
        _build(
            contract_yaml,
            kind="boolean",
            precision=None,
        )


def test_greenhouse_gas_figure_must_declare_a_boundary(contract_yaml: dict) -> None:
    """A tonne of CO2e without a consolidation boundary is not a disclosure."""
    with pytest.raises(ValidationError, match="consolidation boundary"):
        _build(contract_yaml, unit="tCO2e", precision=0)


def test_greenhouse_gas_figure_with_a_boundary_validates(contract_yaml: dict) -> None:
    contract = _build(contract_yaml, unit="tCO2e", precision=0, boundary="operational_control")
    assert contract.unit == "tCO2e"


# ── Abstention ───────────────────────────────────────────────────────────────


def test_contract_cannot_pre_authorize_an_internal_failure(contract_yaml: dict) -> None:
    """The laundering this system exists to prevent, refused at load time."""
    with pytest.raises(ValidationError) as excinfo:
        _build(contract_yaml, abstention={"allowed_reasons": ["E_RESOLVER_ERROR"]})
    assert "cannot be pre-authorized" in str(excinfo.value)


def test_contract_may_declare_lawful_omissions(contract_yaml: dict) -> None:
    contract = _build(contract_yaml, abstention={"allowed_reasons": ["E_PHASE_IN"]})
    assert contract.abstention.allowed_reasons == ("E_PHASE_IN",)


def test_unknown_reason_code_is_rejected(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="unknown abstention reason"):
        _build(contract_yaml, abstention={"allowed_reasons": ["E_WHATEVER"]})


def test_duplicate_reason_is_rejected(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="duplicate abstention reason"):
        _build(contract_yaml, abstention={"allowed_reasons": ["E_PHASE_IN", "E_PHASE_IN"]})


# ── Evidence and assurance ───────────────────────────────────────────────────


def test_required_evidence_must_name_a_class(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="at least one document class"):
        _build(contract_yaml, evidence={"required": True, "classes": []})


def test_assured_datapoint_cannot_waive_evidence(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="cannot waive its evidence requirement"):
        _build(
            contract_yaml,
            evidence={"required": False, "classes": []},
            assurance={"level": "limited"},
        )


def test_cross_check_without_a_bound_is_rejected(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="needs a relative or absolute bound"):
        _build(contract_yaml, tolerance={"cross_check": ["esrs/whatever.sql"]})


# ── Restatement ──────────────────────────────────────────────────────────────


def test_new_version_without_supersedes_is_rejected(contract_yaml: dict) -> None:
    """A changed contract is a restatement and must say so."""
    with pytest.raises(ValidationError, match="must say so"):
        _build(contract_yaml, version=2)


def test_supersedes_must_point_backwards(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="is not older than"):
        _build(
            contract_yaml,
            version=2,
            supersedes={
                "contract_version": 2,
                "effective_from": "2026-01-01",
                "reason": "Methodology changed to the revised GHG Protocol edition.",
            },
        )


def test_valid_restatement_is_accepted(contract_yaml: dict) -> None:
    contract = _build(
        contract_yaml,
        version=2,
        supersedes={
            "contract_version": 1,
            "effective_from": "2026-01-01",
            "reason": "Methodology changed to the revised GHG Protocol edition.",
        },
    )
    assert contract.supersedes is not None


# ── Structural ───────────────────────────────────────────────────────────────


def test_unknown_field_is_rejected(contract_yaml: dict) -> None:
    """`extra=forbid`: a typo in a contract is a failure, not a silently ignored key."""
    with pytest.raises(ValidationError):
        _build(contract_yaml, tolarance={"relative": 0.01})


def test_contract_is_immutable(contract_yaml: dict) -> None:
    contract = _build(contract_yaml)
    with pytest.raises(ValidationError):
        contract.unit = "GWh"


def test_scalar_resolver_cannot_declare_columns(contract_yaml: dict) -> None:
    with pytest.raises(ValidationError, match="scalar resolver cannot declare columns"):
        _build(
            contract_yaml,
            resolver={
                "kind": "sql",
                "query": "esrs/e1_5_electricity_consumption.sql",
                "returns": "scalar",
                "columns": ["a"],
            },
        )


def test_constant_resolver_demands_a_named_approver(contract_yaml: dict) -> None:
    """A magic number in a regulated report has a human's name on it."""
    with pytest.raises(ValidationError):
        _build(
            contract_yaml,
            resolver={
                "kind": "constant",
                "value": 0.3128,
                "source": "European Environment Agency indicator EN-38",
            },
        )
