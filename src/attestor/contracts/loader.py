"""Loading and cross-checking the contract set.

`model.py` enforces everything a single contract can know about itself. This module enforces
everything that needs the whole set: that a derived expression's operands exist and produce
the dimension it claims, that the derivation graph has no cycles, that every SQL file a
contract names is actually on disk, and that every prompt a narrative depends on exists.

Referential integrity here is deliberate. The failure this prevents is the quiet one: a
query is renamed, the contract still points at the old path, and the resolver falls back to
*something* at runtime. There is no fallback. A dangling reference fails the build.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from attestor.contracts import derivation, units
from attestor.contracts.model import DatapointContract, Standard
from attestor.contracts.units import Dimension

CONTRACTS_DIR = "contracts"
QUERIES_DIR = "queries"
PROMPTS_DIR = "prompts"


@dataclass(frozen=True, slots=True)
class ContractIssue:
    """One problem, located precisely enough to fix without searching."""

    path: Path
    datapoint_id: str
    message: str

    def __str__(self) -> str:
        where = f"{self.path}" if not self.datapoint_id else f"{self.path} [{self.datapoint_id}]"
        return f"{where}: {self.message}"


class ContractSetError(Exception):
    """The contract set is not internally consistent."""

    def __init__(self, issues: list[ContractIssue]) -> None:
        joined = "\n  ".join(str(issue) for issue in issues)
        super().__init__(f"{len(issues)} contract problem(s):\n  {joined}")
        self.issues = issues


@dataclass(frozen=True, slots=True)
class ContractSet:
    """Every datapoint contract in the repository, cross-checked and indexed."""

    contracts: Mapping[str, DatapointContract]
    sources: Mapping[str, Path]
    root: Path = field(default_factory=Path)

    def __iter__(self) -> Iterator[DatapointContract]:
        return iter(self.contracts.values())

    def __len__(self) -> int:
        return len(self.contracts)

    def __contains__(self, datapoint_id: object) -> bool:
        return datapoint_id in self.contracts

    def __getitem__(self, datapoint_id: str) -> DatapointContract:
        try:
            return self.contracts[datapoint_id]
        except KeyError:
            raise KeyError(f"no contract for datapoint {datapoint_id!r}") from None

    def get(self, datapoint_id: str) -> DatapointContract | None:
        return self.contracts.get(datapoint_id)

    def for_standard(self, standard: Standard) -> tuple[DatapointContract, ...]:
        return tuple(c for c in self.contracts.values() if c.standard is standard)

    @property
    def dimensions(self) -> dict[str, Dimension]:
        return {c.id: c.dimension for c in self.contracts.values()}

    def resolution_order(self) -> tuple[str, ...]:
        """Datapoint ids in an order where every derived operand precedes its dependant."""
        return tuple(_topological_order(self.contracts))


def load(root: Path | str = ".", *, strict: bool = True) -> ContractSet:
    """Load every contract under `root/contracts`, cross-check, and index.

    With `strict=True` (the default, and what CI uses) any issue raises. `strict=False` is
    for tooling that wants to report on a broken set rather than die on it.
    """
    root = Path(root)
    contracts_dir = root / CONTRACTS_DIR
    if not contracts_dir.is_dir():
        raise FileNotFoundError(f"no contracts directory at {contracts_dir}")

    contracts: dict[str, DatapointContract] = {}
    sources: dict[str, Path] = {}
    issues: list[ContractIssue] = []

    for path in sorted(contracts_dir.rglob("*.yaml")):
        if path.parent.name == "schema":
            continue
        relative = path.relative_to(root)
        raw = _read_yaml(path)
        if raw is None:
            issues.append(ContractIssue(relative, "", "file is empty"))
            continue
        if not isinstance(raw, dict):
            issues.append(ContractIssue(relative, "", "top level must be a mapping"))
            continue
        try:
            contract = DatapointContract.model_validate(raw)
        except Exception as exc:  # pydantic raises ValidationError; surface it verbatim
            issues.append(ContractIssue(relative, str(raw.get("id", "")), _tidy(exc)))
            continue
        if contract.id in contracts:
            issues.append(
                ContractIssue(
                    relative,
                    contract.id,
                    f"duplicate datapoint id, already defined in {sources[contract.id]}",
                )
            )
            continue
        contracts[contract.id] = contract
        sources[contract.id] = relative

    issues.extend(_cross_check(contracts, sources, root))

    if issues and strict:
        raise ContractSetError(issues)
    return ContractSet(contracts=contracts, sources=sources, root=root)


def validate(root: Path | str = ".") -> list[ContractIssue]:
    """Return every issue in the contract set without raising. Used by the CLI."""
    try:
        load(root, strict=True)
    except ContractSetError as exc:
        return exc.issues
    return []


# ── Cross-checks ─────────────────────────────────────────────────────────────


def _cross_check(
    contracts: Mapping[str, DatapointContract],
    sources: Mapping[str, Path],
    root: Path,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    dimensions = {c.id: c.dimension for c in contracts.values()}

    for datapoint_id, contract in contracts.items():
        where = sources[datapoint_id]
        resolver = contract.resolver

        match resolver.kind:
            case "sql":
                issues += _check_sql(contract, where, root)
            case "derived":
                issues += _check_derived(contract, where, contracts, dimensions)
            case "narrative":
                issues += _check_narrative(contract, where, root)
            case "constant":
                pass  # fully validated in isolation

        issues += _check_cross_check_queries(contract, where, root)

    issues += _check_no_cycles(contracts, sources)
    return issues


def _check_sql(contract: DatapointContract, where: Path, root: Path) -> list[ContractIssue]:
    query_path = root / QUERIES_DIR / contract.resolver.query
    if not query_path.is_file():
        return [
            ContractIssue(
                where,
                contract.id,
                f"resolver names {QUERIES_DIR}/{contract.resolver.query}, which does not exist",
            )
        ]
    return []


def _check_cross_check_queries(
    contract: DatapointContract, where: Path, root: Path
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    for query in contract.tolerance.cross_check:
        if not (root / QUERIES_DIR / query).is_file():
            issues.append(
                ContractIssue(
                    where, contract.id, f"cross-check query {QUERIES_DIR}/{query} does not exist"
                )
            )
    return issues


def _check_derived(
    contract: DatapointContract,
    where: Path,
    contracts: Mapping[str, DatapointContract],
    dimensions: Mapping[str, Dimension],
) -> list[ContractIssue]:
    expression = contract.resolver.expression
    issues: list[ContractIssue] = []

    try:
        referenced = derivation.referenced_datapoints(expression)
    except derivation.InvalidExpression as exc:
        return [ContractIssue(where, contract.id, str(exc))]

    missing = sorted(name for name in referenced if name not in contracts)
    if missing:
        return [
            ContractIssue(
                where,
                contract.id,
                f"expression references undefined datapoint(s): {', '.join(missing)}",
            )
        ]

    model_authored = sorted(name for name in referenced if contracts[name].is_model_authored)
    if model_authored:
        issues.append(
            ContractIssue(
                where,
                contract.id,
                f"expression depends on model-authored datapoint(s): {', '.join(model_authored)}"
                " — a figure cannot be derived from generated prose",
            )
        )

    # An aggregate must be able to say what its components can say. If Scope 3 may lawfully
    # be phased in but the total may not, then the day Scope 3 is omitted the total abstains
    # with a reason it never declared — and the resolver turns that into a block. Catching it
    # here means the inconsistency surfaces in review rather than on the deadline.
    for operand_id in sorted(referenced):
        operand = contracts[operand_id]
        undeclared = set(operand.abstention.allowed_reasons) - set(
            contract.abstention.allowed_reasons
        )
        if undeclared:
            issues.append(
                ContractIssue(
                    where,
                    contract.id,
                    f"operand {operand_id} may lawfully omit for "
                    f"{', '.join(sorted(undeclared))}, which this datapoint does not declare — "
                    "an aggregate must be able to state what its components can",
                )
            )

    try:
        actual = derivation.infer_dimension(expression, dict(dimensions))
    except (units.DimensionMismatch, derivation.InvalidExpression) as exc:
        return [*issues, ContractIssue(where, contract.id, str(exc))]

    expected = contract.dimension
    if actual != expected:
        issues.append(
            ContractIssue(
                where,
                contract.id,
                f"declared unit {contract.unit!r} has dimension "
                f"{units.format_dimension(expected)}, but the expression produces "
                f"{units.format_dimension(actual)}",
            )
        )
    return issues


def _check_narrative(contract: DatapointContract, where: Path, root: Path) -> list[ContractIssue]:
    prompt_path = root / PROMPTS_DIR / f"{contract.resolver.prompt_id}.md"
    if not prompt_path.is_file():
        return [
            ContractIssue(
                where,
                contract.id,
                f"narrative resolver names prompt {contract.resolver.prompt_id!r}, "
                f"but {PROMPTS_DIR}/{contract.resolver.prompt_id}.md does not exist",
            )
        ]
    return []


def _check_no_cycles(
    contracts: Mapping[str, DatapointContract], sources: Mapping[str, Path]
) -> list[ContractIssue]:
    try:
        _topological_order(contracts)
    except _CycleError as exc:
        first = exc.cycle[0]
        return [
            ContractIssue(
                sources.get(first, Path("contracts")),
                first,
                f"derivation cycle: {' → '.join(exc.cycle)}",
            )
        ]
    return []


class _CycleError(Exception):
    def __init__(self, cycle: list[str]) -> None:
        super().__init__(" → ".join(cycle))
        self.cycle = cycle


def _topological_order(contracts: Mapping[str, DatapointContract]) -> list[str]:
    """Kahn's algorithm over the derivation graph, with a readable cycle on failure."""
    dependencies: dict[str, frozenset[str]] = {}
    for datapoint_id, contract in contracts.items():
        if contract.resolver.kind == "derived":
            try:
                refs = derivation.referenced_datapoints(contract.resolver.expression)
            except derivation.InvalidExpression:
                refs = frozenset()
            dependencies[datapoint_id] = frozenset(r for r in refs if r in contracts)
        else:
            dependencies[datapoint_id] = frozenset()

    ordered: list[str] = []
    remaining = dict(dependencies)
    while remaining:
        ready = sorted(node for node, deps in remaining.items() if not (deps - set(ordered)))
        if not ready:
            raise _CycleError(_find_cycle(remaining))
        ordered.extend(ready)
        for node in ready:
            remaining.pop(node)
    return ordered


def _find_cycle(graph: Mapping[str, frozenset[str]]) -> list[str]:
    """Walk the residual graph to name an actual cycle, not just 'a cycle exists'."""
    start = min(graph)
    seen: list[str] = []
    node = start
    while node not in seen:
        seen.append(node)
        candidates = sorted(dep for dep in graph.get(node, frozenset()) if dep in graph)
        if not candidates:
            break
        node = candidates[0]
    if node in seen:
        return [*seen[seen.index(node) :], node]
    return seen


# ── I/O helpers ──────────────────────────────────────────────────────────────


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _tidy(exc: Exception) -> str:
    """Collapse a pydantic ValidationError into one actionable line per problem."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc).replace("\n", " ")
    lines = []
    for error in errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg", "")
        message = message.removeprefix("Value error, ")
        lines.append(f"{location}: {message}" if location else message)
    return "; ".join(lines)


def repository_root(start: Path | str | None = None) -> Path:
    """Find the repository root by walking up to the directory holding `contracts/`."""
    current = Path(start or os.getcwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONTRACTS_DIR).is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise FileNotFoundError(f"no attestor repository root above {current}")
