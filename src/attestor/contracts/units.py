"""A small unit registry with real dimensional analysis.

Regulated figures are wrong in a specific, boring way: someone divides tonnes of CO2 by
revenue and labels the result `tCO2e`, or sums megawatt-hours into a gigajoule total without
converting. Nobody notices, because the number looks plausible and prose cannot check
arithmetic.

So units are a closed vocabulary with declared dimensions, and a derived datapoint's
*declared* unit is checked against the dimension its expression actually produces. A
mismatch is a load-time failure, not a footnote.

The registry is deliberately small. It covers what the ESRS quantitative datapoints in this
repository actually use. Adding a unit is a reviewed change, same as adding a reason code.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Self

#: A dimension is an exponent per base quantity. `{}` is dimensionless (ratios, counts).
Dimension = dict[str, Fraction]


def _dim(**kwargs: int | Fraction) -> Dimension:
    return {k: Fraction(v) for k, v in kwargs.items() if v}


@dataclass(frozen=True, slots=True)
class Unit:
    """A unit of measure, its dimension, and how it relates to the canonical unit."""

    symbol: str
    dimension: Dimension
    #: Multiplier to the canonical unit of the same dimension. Exact where it can be:
    #: unit conversion inside a regulated figure must not accumulate float error.
    to_canonical: Fraction
    description: str

    @property
    def is_dimensionless(self) -> bool:
        return not self.dimension


# Base quantities used by the datapoints in this repository.
_MASS_CO2E = "mass_co2e"  # kept distinct from plain mass: tCO2e is not interchangeable with t
_ENERGY = "energy"
_VOLUME = "volume"
_MONEY = "money"
_PEOPLE = "people"
_TIME = "time"
_DISTANCE = "distance"
_MASS = "mass"

_UNITS: tuple[Unit, ...] = (
    # Greenhouse gases — canonical: tCO2e
    Unit("tCO2e", _dim(**{_MASS_CO2E: 1}), Fraction(1), "tonnes of CO2 equivalent"),
    Unit("ktCO2e", _dim(**{_MASS_CO2E: 1}), Fraction(1000), "kilotonnes of CO2 equivalent"),
    Unit("kgCO2e", _dim(**{_MASS_CO2E: 1}), Fraction(1, 1000), "kilograms of CO2 equivalent"),
    # Energy — canonical: MWh
    Unit("MWh", _dim(**{_ENERGY: 1}), Fraction(1), "megawatt hours"),
    Unit("GWh", _dim(**{_ENERGY: 1}), Fraction(1000), "gigawatt hours"),
    Unit("kWh", _dim(**{_ENERGY: 1}), Fraction(1, 1000), "kilowatt hours"),
    Unit("GJ", _dim(**{_ENERGY: 1}), Fraction(1000, 3600), "gigajoules"),
    # Water — canonical: m3
    Unit("m3", _dim(**{_VOLUME: 1}), Fraction(1), "cubic metres"),
    Unit("ML", _dim(**{_VOLUME: 1}), Fraction(1000), "megalitres"),
    # Waste and materials — canonical: t
    Unit("t", _dim(**{_MASS: 1}), Fraction(1), "metric tonnes"),
    Unit("kg", _dim(**{_MASS: 1}), Fraction(1, 1000), "kilograms"),
    # Money — canonical: EUR
    Unit("EUR", _dim(**{_MONEY: 1}), Fraction(1), "euro"),
    Unit("kEUR", _dim(**{_MONEY: 1}), Fraction(1000), "thousand euro"),
    Unit("MEUR", _dim(**{_MONEY: 1}), Fraction(1_000_000), "million euro"),
    # Workforce
    Unit("headcount", _dim(**{_PEOPLE: 1}), Fraction(1), "number of people"),
    Unit("FTE", _dim(**{_PEOPLE: 1}), Fraction(1), "full-time equivalents"),
    Unit("hours", _dim(**{_TIME: 1}), Fraction(1), "hours"),
    # Transport
    Unit("km", _dim(**{_DISTANCE: 1}), Fraction(1), "kilometres"),
    Unit("tkm", _dim(**{_MASS: 1, _DISTANCE: 1}), Fraction(1), "tonne-kilometres"),
    # Compound units. Explicit rather than composed on the fly: a published intensity is a
    # reviewed unit, and "we generated it" is not a review.
    Unit(
        "tCO2e/MWh",
        _dim(**{_MASS_CO2E: 1, _ENERGY: -1}),
        Fraction(1),
        "emission factor per unit of energy",
    ),
    Unit("kgCO2e/kWh", _dim(**{_MASS_CO2E: 1, _ENERGY: -1}), Fraction(1), "emission factor"),
    Unit(
        "gCO2e/kWh",
        _dim(**{_MASS_CO2E: 1, _ENERGY: -1}),
        Fraction(1, 1000),
        "emission factor, grid convention",
    ),
    Unit("tCO2e/EUR", _dim(**{_MASS_CO2E: 1, _MONEY: -1}), Fraction(1), "emissions per euro"),
    Unit(
        "tCO2e/MEUR",
        _dim(**{_MASS_CO2E: 1, _MONEY: -1}),
        Fraction(1, 1_000_000),
        "GHG intensity per million euro of net revenue (ESRS E1-6 §53)",
    ),
    Unit(
        "MWh/MEUR",
        _dim(**{_ENERGY: 1, _MONEY: -1}),
        Fraction(1, 1_000_000),
        "energy intensity per million euro of net revenue (ESRS E1-5)",
    ),
    Unit(
        "gCO2e/tkm",
        _dim(**{_MASS_CO2E: 1, _MASS: -1, _DISTANCE: -1}),
        Fraction(1, 1_000_000),
        "freight transport emission intensity",
    ),
    Unit(
        "tCO2e/tkm",
        _dim(**{_MASS_CO2E: 1, _MASS: -1, _DISTANCE: -1}),
        Fraction(1),
        "freight transport emission intensity, canonical",
    ),
    # Dimensionless
    Unit("ratio", {}, Fraction(1), "a pure ratio in [0, 1]"),
    Unit("percent", {}, Fraction(1, 100), "percentage points"),
    Unit("count", {}, Fraction(1), "a dimensionless count of items"),
)

BY_SYMBOL: dict[str, Unit] = {u.symbol: u for u in _UNITS}
ALL_SYMBOLS: frozenset[str] = frozenset(BY_SYMBOL)


class UnknownUnit(ValueError):
    def __init__(self, symbol: str) -> None:
        super().__init__(
            f"unknown unit {symbol!r}; the registry is closed: {', '.join(sorted(ALL_SYMBOLS))}"
        )
        self.symbol = symbol


class DimensionMismatch(ValueError):
    """A declared unit does not match the dimension its expression produces."""

    def __init__(self, *, declared: str, expected: Dimension, actual: Dimension) -> None:
        super().__init__(
            f"declared unit {declared!r} has dimension {format_dimension(expected)}, "
            f"but the expression produces {format_dimension(actual)}"
        )
        self.declared = declared
        self.expected = expected
        self.actual = actual


def resolve(symbol: str) -> Unit:
    try:
        return BY_SYMBOL[symbol]
    except KeyError:
        raise UnknownUnit(symbol) from None


def format_dimension(dimension: Dimension) -> str:
    """Render a dimension for an error message: ``mass_co2e / money`` and so on."""
    if not dimension:
        return "dimensionless"
    numerator = [b for b, e in sorted(dimension.items()) if e > 0 for _ in range(1)]
    parts: list[str] = []
    for base in numerator:
        exponent = dimension[base]
        parts.append(base if exponent == 1 else f"{base}^{exponent}")
    denominator = [b for b, e in sorted(dimension.items()) if e < 0]
    if not parts:
        parts.append("1")
    result = " · ".join(parts)
    if denominator:
        denom_parts = [
            base if -dimension[base] == 1 else f"{base}^{-dimension[base]}" for base in denominator
        ]
        result = f"{result} / {' · '.join(denom_parts)}"
    return result


def multiply(left: Dimension, right: Dimension) -> Dimension:
    out: Dimension = dict(left)
    for base, exponent in right.items():
        combined = out.get(base, Fraction(0)) + exponent
        if combined:
            out[base] = combined
        else:
            out.pop(base, None)
    return out


def divide(left: Dimension, right: Dimension) -> Dimension:
    return multiply(left, {base: -exp for base, exp in right.items()})


def add(left: Dimension, right: Dimension) -> Dimension:
    """Addition requires identical dimensions. Summing MWh into tCO2e is not a rounding error."""
    if left != right:
        raise DimensionMismatch(declared="<sum>", expected=left, actual=right)
    return dict(left)


@dataclass(frozen=True, slots=True)
class Quantity:
    """A value with a unit. Conversions go through exact rationals, never floats."""

    value: Fraction
    unit: Unit

    @classmethod
    def of(cls, value: float | int | str | Fraction, symbol: str) -> Self:
        return cls(
            Fraction(str(value)) if isinstance(value, float) else Fraction(value), resolve(symbol)
        )

    def to(self, symbol: str) -> Self:
        target = resolve(symbol)
        if target.dimension != self.unit.dimension:
            raise DimensionMismatch(
                declared=symbol, expected=target.dimension, actual=self.unit.dimension
            )
        canonical = self.value * self.unit.to_canonical
        return type(self)(canonical / target.to_canonical, target)

    def __str__(self) -> str:
        return f"{float(self.value)} {self.unit.symbol}"
