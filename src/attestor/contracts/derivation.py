"""Safe arithmetic over datapoints.

Some regulated figures are defined in terms of others: location-based Scope 2 is metered
electricity times a grid factor; GHG intensity is total emissions over net revenue. Writing
that as Python would put a published number behind an import; writing it as a string and
calling `eval` would put it behind arbitrary code execution. Neither is acceptable for a
figure an auditor will sign.

So a derived datapoint declares an expression like::

    {ESRS_E1-5_electricity_consumption} * {ESRS_E1_grid_factor_GR}

Datapoint references are braced. This is not decoration: ESRS identifiers contain hyphens
(``E1-5``), which Python's tokenizer reads as subtraction. Braces make a reference
unambiguous, and they make the stricter rule below expressible — after substitution, *any*
remaining bare name is rejected, so an expression cannot reach for a name that is not a
declared datapoint.

What is accepted: ``+ - * /``, unary minus, numeric literals, parentheses, braced datapoint
references. Nothing else. No attribute access, no calls, no subscripts, no comparisons.
"""

from __future__ import annotations

import ast
import re
from fractions import Fraction

from attestor.contracts import units
from attestor.contracts.units import Dimension

#: A braced datapoint reference. The inner pattern mirrors `model.DATAPOINT_ID`.
REFERENCE = re.compile(r"\{([A-Z][A-Za-z0-9_.\-]*)\}")

_SAFE_PREFIX = "_dp_"

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.USub,
    ast.UAdd,
    ast.Name,
    ast.Load,
    ast.Constant,
    *_ALLOWED_BINOPS,
)


class InvalidExpression(ValueError):
    """The expression is not a plain arithmetic combination of braced datapoint references."""


class UnresolvedReference(KeyError):
    """The expression names a datapoint that was not supplied."""

    def __init__(self, name: str) -> None:
        super().__init__(f"expression references unknown datapoint {name!r}")
        self.name = name


def _safe_name(index: int) -> str:
    return f"{_SAFE_PREFIX}{index}"


def _normalise(expression: str) -> tuple[ast.Expression, dict[str, str]]:
    """Replace braced references with safe identifiers, then parse and whitelist.

    Returns the parsed tree and a mapping from the generated identifier to the datapoint id.
    """
    if "{" in expression and not REFERENCE.search(expression):
        raise InvalidExpression(
            f"{expression!r} contains an opening brace but no valid datapoint reference; "
            "references look like {ESRS_E1-5_electricity_consumption}"
        )

    mapping: dict[str, str] = {}
    counter = 0

    def substitute(match: re.Match[str]) -> str:
        nonlocal counter
        datapoint_id = match.group(1)
        for existing, known in mapping.items():
            if known == datapoint_id:
                return existing
        identifier = _safe_name(counter)
        counter += 1
        mapping[identifier] = datapoint_id
        return identifier

    normalised = REFERENCE.sub(substitute, expression)
    if "}" in normalised or "{" in normalised:
        raise InvalidExpression(f"{expression!r} has an unbalanced datapoint reference")

    try:
        tree = ast.parse(normalised, mode="eval")
    except SyntaxError as exc:
        raise InvalidExpression(f"{expression!r} is not a valid expression: {exc.msg}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise InvalidExpression(
                f"{expression!r} contains {type(node).__name__}, which is not permitted; "
                "only + - * / over braced datapoint references and numeric literals are allowed"
            )
        if isinstance(node, ast.Constant) and not isinstance(node.value, int | float):
            raise InvalidExpression(f"{expression!r} contains a non-numeric literal")
        if isinstance(node, ast.Name) and node.id not in mapping:
            raise InvalidExpression(
                f"{expression!r} contains the bare name {node.id!r}; "
                "datapoints must be referenced in braces"
            )
    return tree, mapping


def parse(expression: str) -> ast.Expression:
    """Parse and whitelist an expression, discarding the reference mapping."""
    tree, _ = _normalise(expression)
    return tree


def referenced_datapoints(expression: str) -> frozenset[str]:
    """The datapoint identifiers an expression depends on."""
    _, mapping = _normalise(expression)
    return frozenset(mapping.values())


def infer_dimension(expression: str, dimensions: dict[str, Dimension]) -> Dimension:
    """Infer the dimension an expression produces from the dimensions of its operands.

    A numeric literal is dimensionless. Addition and subtraction demand identical dimensions
    on both sides; multiplication and division combine them. This is what stops megawatt-hours
    being summed into tonnes of CO2 equivalent.
    """
    tree, mapping = _normalise(expression)
    resolved = {
        identifier: _lookup(dimensions, datapoint_id)
        for identifier, datapoint_id in mapping.items()
    }
    return _walk_dimension(tree.body, resolved)


def evaluate(expression: str, values: dict[str, Fraction]) -> Fraction:
    """Evaluate an expression over exact rationals.

    Values arrive keyed by datapoint id, already converted to the **canonical** unit of
    their dimension — see `units.Quantity.to`. Working in canonical units is what makes the
    dimensional check sufficient: once every operand is canonical, a dimensionally correct
    expression is also numerically correct, and the caller converts the result to the
    declared unit at the end.

    Division by zero surfaces as `ZeroDivisionError` rather than a silent NaN. A regulated
    figure has no NaN.
    """
    tree, mapping = _normalise(expression)
    resolved = {
        identifier: _lookup(values, datapoint_id) for identifier, datapoint_id in mapping.items()
    }
    return _walk_value(tree.body, resolved)


def _lookup[T](source: dict[str, T], datapoint_id: str) -> T:
    try:
        return source[datapoint_id]
    except KeyError:
        raise UnresolvedReference(datapoint_id) from None


def _walk_dimension(node: ast.expr, dimensions: dict[str, Dimension]) -> Dimension:
    match node:
        case ast.Constant():
            return {}
        case ast.Name(id=name):
            return dimensions[name]
        case ast.UnaryOp(operand=operand):
            return _walk_dimension(operand, dimensions)
        case ast.BinOp(left=left, op=op, right=right):
            left_dim = _walk_dimension(left, dimensions)
            right_dim = _walk_dimension(right, dimensions)
            match op:
                case ast.Add() | ast.Sub():
                    return units.add(left_dim, right_dim)
                case ast.Mult():
                    return units.multiply(left_dim, right_dim)
                case ast.Div():
                    return units.divide(left_dim, right_dim)
    raise InvalidExpression(f"unsupported node {type(node).__name__}")


def _walk_value(node: ast.expr, values: dict[str, Fraction]) -> Fraction:
    match node:
        case ast.Constant(value=value):
            return Fraction(str(value)) if isinstance(value, float) else Fraction(value)
        case ast.Name(id=name):
            return values[name]
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_walk_value(operand, values)
        case ast.UnaryOp(op=ast.UAdd(), operand=operand):
            return _walk_value(operand, values)
        case ast.BinOp(left=left, op=op, right=right):
            lhs = _walk_value(left, values)
            rhs = _walk_value(right, values)
            match op:
                case ast.Add():
                    return lhs + rhs
                case ast.Sub():
                    return lhs - rhs
                case ast.Mult():
                    return lhs * rhs
                case ast.Div():
                    if rhs == 0:
                        raise ZeroDivisionError(
                            "derived datapoint divides by zero; a regulated figure has no NaN"
                        )
                    return lhs / rhs
    raise InvalidExpression(f"unsupported node {type(node).__name__}")
