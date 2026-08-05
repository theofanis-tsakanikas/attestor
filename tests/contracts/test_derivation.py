"""The expression whitelist is the reason a published number is not arbitrary code.

The parametrised rejection list below is not paranoia. Each entry is a way a string that
looks like arithmetic can reach out of arithmetic: a call, an attribute, a subscript, a
comprehension, a walrus. `eval` accepts all of them.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from attestor.contracts import derivation, units
from attestor.contracts.derivation import InvalidExpression, UnresolvedReference

DIMENSIONS = {
    "ESRS_E1-5_electricity_consumption": units.resolve("MWh").dimension,
    "ESRS_E1_grid_factor_GR": units.resolve("tCO2e/MWh").dimension,
    "ESRS_E1-6_gross_scope_1": units.resolve("tCO2e").dimension,
    "ESRS_E1-6_net_revenue": units.resolve("MEUR").dimension,
}


def test_hyphenated_identifiers_survive_parsing() -> None:
    """ESRS ids contain hyphens; Python reads those as subtraction. Braces are why this works."""
    assert derivation.referenced_datapoints("{ESRS_E1-5_electricity_consumption}") == frozenset(
        {"ESRS_E1-5_electricity_consumption"}
    )


def test_repeated_reference_maps_to_one_datapoint() -> None:
    expression = "{ESRS_E1-6_gross_scope_1} + {ESRS_E1-6_gross_scope_1}"
    assert derivation.referenced_datapoints(expression) == frozenset({"ESRS_E1-6_gross_scope_1"})
    assert derivation.evaluate(expression, {"ESRS_E1-6_gross_scope_1": Fraction(21)}) == 42


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('rm -rf /')",
        "{ESRS_E1-6_gross_scope_1}.__class__",
        "open('/etc/passwd').read()",
        "[x for x in range(10)]",
        "{ESRS_E1-6_gross_scope_1}[0]",
        "abs({ESRS_E1-6_gross_scope_1})",
        "{ESRS_E1-6_gross_scope_1} if True else 0",
        "{ESRS_E1-6_gross_scope_1} ** 2",
        "{ESRS_E1-6_gross_scope_1} > 0",
        "(y := {ESRS_E1-6_gross_scope_1})",
        "lambda: 1",
    ],
)
def test_expression_whitelist_rejects_everything_that_is_not_arithmetic(expression: str) -> None:
    with pytest.raises(InvalidExpression):
        derivation.parse(expression)


def test_bare_names_are_rejected() -> None:
    """An unbraced name could resolve to anything. It resolves to nothing."""
    with pytest.raises(InvalidExpression) as excinfo:
        derivation.parse("{ESRS_E1-6_gross_scope_1} + secret_total")
    assert "braces" in str(excinfo.value)


def test_unbalanced_reference_is_rejected() -> None:
    with pytest.raises(InvalidExpression):
        derivation.parse("{ESRS_E1-6_gross_scope_1 + 1")


def test_string_literals_are_rejected() -> None:
    with pytest.raises(InvalidExpression):
        derivation.parse("'1000'")


# ── Dimensional inference ────────────────────────────────────────────────────


def test_energy_times_factor_is_emissions() -> None:
    inferred = derivation.infer_dimension(
        "{ESRS_E1-5_electricity_consumption} * {ESRS_E1_grid_factor_GR}", DIMENSIONS
    )
    assert inferred == units.resolve("tCO2e").dimension


def test_emissions_over_revenue_is_intensity() -> None:
    inferred = derivation.infer_dimension(
        "{ESRS_E1-6_gross_scope_1} / {ESRS_E1-6_net_revenue}", DIMENSIONS
    )
    assert inferred == units.resolve("tCO2e/MEUR").dimension


def test_adding_energy_to_emissions_is_a_dimension_error() -> None:
    with pytest.raises(units.DimensionMismatch):
        derivation.infer_dimension(
            "{ESRS_E1-5_electricity_consumption} + {ESRS_E1-6_gross_scope_1}", DIMENSIONS
        )


def test_numeric_literals_are_dimensionless() -> None:
    inferred = derivation.infer_dimension("{ESRS_E1-6_gross_scope_1} * 2", DIMENSIONS)
    assert inferred == units.resolve("tCO2e").dimension


def test_unknown_operand_is_named_in_the_error() -> None:
    with pytest.raises(UnresolvedReference) as excinfo:
        derivation.infer_dimension("{ESRS_MISSING_thing}", DIMENSIONS)
    assert "ESRS_MISSING_thing" in str(excinfo.value)


# ── Evaluation ───────────────────────────────────────────────────────────────


def test_evaluation_is_exact() -> None:
    values = {
        "ESRS_E1-5_electricity_consumption": Fraction("12345.6"),
        "ESRS_E1_grid_factor_GR": Fraction("0.3128"),
    }
    result = derivation.evaluate(
        "{ESRS_E1-5_electricity_consumption} * {ESRS_E1_grid_factor_GR}", values
    )
    assert result == Fraction("12345.6") * Fraction("0.3128")
    assert isinstance(result, Fraction)


def test_division_by_zero_raises_rather_than_producing_nan() -> None:
    values = {"ESRS_E1-6_gross_scope_1": Fraction(100), "ESRS_E1-6_net_revenue": Fraction(0)}
    with pytest.raises(ZeroDivisionError) as excinfo:
        derivation.evaluate("{ESRS_E1-6_gross_scope_1} / {ESRS_E1-6_net_revenue}", values)
    assert "NaN" in str(excinfo.value)


def test_unary_minus_is_allowed() -> None:
    assert derivation.evaluate("-{A_x}", {"A_x": Fraction(5)}) == -5
