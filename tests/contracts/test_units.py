"""Unit algebra: dimensions combine, conversions are exact, and mismatches are fatal."""

from __future__ import annotations

from fractions import Fraction

import pytest

from attestor.contracts import units
from attestor.contracts.units import DimensionMismatch, Quantity, UnknownUnit


def test_registry_is_closed() -> None:
    with pytest.raises(UnknownUnit) as excinfo:
        units.resolve("bananas")
    assert "closed" in str(excinfo.value)


def test_co2e_is_not_interchangeable_with_mass() -> None:
    """A tonne of CO2e and a tonne of waste are not the same quantity, and adding them is a bug."""
    assert units.resolve("tCO2e").dimension != units.resolve("t").dimension
    with pytest.raises(DimensionMismatch):
        Quantity.of(1, "tCO2e").to("t")


def test_conversion_is_exact() -> None:
    """Regulated figures do not accumulate float error on the way to the page."""
    assert Quantity.of(1, "GWh").to("MWh").value == Fraction(1000)
    assert Quantity.of(1, "kWh").to("MWh").value == Fraction(1, 1000)
    assert Quantity.of(3, "GJ").to("MWh").value == Fraction(3000, 3600)


def test_round_trip_returns_the_original_rational() -> None:
    original = Quantity.of("1234.5678", "kWh")
    assert original.to("MWh").to("kWh").value == original.value


def test_multiplication_combines_dimensions() -> None:
    energy = units.resolve("MWh").dimension
    factor = units.resolve("tCO2e/MWh").dimension
    assert units.multiply(energy, factor) == units.resolve("tCO2e").dimension


def test_division_produces_intensity() -> None:
    emissions = units.resolve("tCO2e").dimension
    money = units.resolve("MEUR").dimension
    assert units.divide(emissions, money) == units.resolve("tCO2e/MEUR").dimension


def test_addition_demands_identical_dimensions() -> None:
    with pytest.raises(DimensionMismatch):
        units.add(units.resolve("MWh").dimension, units.resolve("tCO2e").dimension)


def test_grid_factor_conventions_agree() -> None:
    """kgCO2e/kWh and tCO2e/MWh are the same number, which is why both are in the registry."""
    assert Quantity.of("0.3128", "kgCO2e/kWh").to("tCO2e/MWh").value == Fraction("0.3128")


def test_gco2e_per_kwh_is_a_thousandfold_smaller() -> None:
    """The conversion this whole module exists to get right."""
    assert Quantity.of(312.8, "gCO2e/kWh").to("tCO2e/MWh").value == Fraction("0.3128")


def test_percent_is_dimensionless_but_scaled() -> None:
    assert units.resolve("percent").is_dimensionless
    assert Quantity.of(50, "percent").to("ratio").value == Fraction(1, 2)


def test_dimension_formatting_is_readable() -> None:
    assert units.format_dimension({}) == "dimensionless"
    assert units.format_dimension(units.resolve("tCO2e/MEUR").dimension) == "mass_co2e / money"
