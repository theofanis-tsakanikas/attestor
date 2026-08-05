"""When an extracted value may support a published figure, and when it may only be evidence.

This is the control the whole extraction path exists to be safe under.

An OCR engine that reads `1` as `7` produces a number that is plausible, well-formed, and
wrong. There is no confidence threshold that fixes it — a high-confidence misread is still a
misread, and the errors that matter are exactly the ones the reader was sure about. So good
faith is not available here, and neither is care: the question has to be answered by
structure.

The structure is already in the contract. `tolerance.cross_check` names an independent
computation of the same figure that must land inside a declared bound. Where a contract
declares one, a misread digit moves the primary away from its cross-check and the resolver
refuses with `E_OUT_OF_TOLERANCE` — the existing mechanism, unchanged, doing the job it was
built for. Where a contract declares none, nothing would catch the misread, so an extracted
value may not reach the figure at all: it counts as evidence coverage, which is what a
scanned invoice honestly is, and the figure comes from somewhere else or is not stated.

**Which side.** The reconciliation only means something if the extracted value sits on one
side of it and an independent *system* path on the other. Reconciling OCR against OCR proves
that two readings of the same paper agree, which is not the claim anyone needs. So a dataset
is admissible for a datapoint when it backs that datapoint's cross-check and the primary
resolver reads elsewhere — never the reverse. For `ESRS_E1-6_gross_scope_1` that means
telematics is primary and the fuel invoice is the cross-check, which is also the direction
the queries were already written in.

Everything here is a pure function of the contract set, deliberately. It runs offline, in
CI, and inside a `gate_proof` mutation — a rule that could only be evaluated against a
warehouse would be a rule this repository cannot prove.
"""

from __future__ import annotations

from dataclasses import dataclass

from attestor.contracts.loader import ContractSet
from attestor.contracts.model import DatapointContract
from attestor.datapoints.backends import tables_in


class InadmissibleExtraction(ValueError):
    """An extracted value was routed at a figure nothing would reconcile it against."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether extracted rows may reach this datapoint's figure, and why."""

    datapoint_id: str
    admissible: bool
    reason: str
    #: The cross-check that reconciles it, when one does.
    reconciled_by: str = ""

    def __bool__(self) -> bool:
        return self.admissible


def judge(
    contract: DatapointContract,
    *,
    dataset: str,
    contracts: ContractSet,
) -> Verdict:
    """May extracted rows in `dataset` support this datapoint's published figure?

    `dataset` is a qualified table name — `gold.procurement_fuel_spend` — because that is
    what the queries name and what the ingestion writes. Matching on anything softer would
    make the rule depend on a label somebody maintains by hand.
    """
    if contract.resolver.kind != "sql":
        return Verdict(
            contract.id,
            False,
            f"{contract.id} does not resolve through SQL, so no extracted row reaches it",
        )

    tolerance = contract.tolerance
    if not tolerance.cross_check:
        return Verdict(
            contract.id,
            False,
            f"{contract.id} declares no cross-check. An extracted value reaching this figure "
            "would be a misread nothing reconciles, so extracted rows count as evidence "
            "coverage and the figure comes from elsewhere",
        )

    primary = _tables(contracts, contract.resolver.query)
    if dataset in primary:
        return Verdict(
            contract.id,
            False,
            f"{dataset} backs the primary resolver for {contract.id}. Extracted rows belong "
            "on the cross-check side: reconciling a reading of the paper against the same "
            "reading proves the reader is consistent, not that the figure is right",
        )

    for query in tolerance.cross_check:
        if dataset in _tables(contracts, query):
            return Verdict(
                contract.id,
                True,
                f"{dataset} backs the cross-check for {contract.id}, reconciled against an "
                f"independent path within {_bound(contract)}",
                reconciled_by=query,
            )

    return Verdict(
        contract.id,
        False,
        f"{dataset} backs neither the resolver nor a cross-check of {contract.id}",
    )


def admissible_datapoints(dataset: str, contracts: ContractSet) -> tuple[str, ...]:
    """Every datapoint whose published figure extracted rows in `dataset` may support."""
    return tuple(
        sorted(
            contract.id
            for contract in contracts
            if judge(contract, dataset=dataset, contracts=contracts)
        )
    )


def require_admissible(dataset: str, contracts: ContractSet) -> tuple[str, ...]:
    """Assert that extracted rows in `dataset` are reconciled somewhere, or refuse.

    Called by the ingestion before a single extracted row is written to a dataset a published
    figure reads. Writing them and hoping the cross-check catches a misread later is the same
    mistake as publishing and hoping review catches it.
    """
    allowed = admissible_datapoints(dataset, contracts)
    if not allowed:
        raise InadmissibleExtraction(
            f"no datapoint reconciles extracted rows in {dataset} against an independent "
            "path. Extracted values may support a published figure only behind a declared "
            "tolerance.cross_check; without one they are evidence coverage and nothing more."
        )
    return allowed


def _tables(contracts: ContractSet, query: str) -> frozenset[str]:
    """The tables a committed query reads.

    Parsed from the statement rather than declared beside it, so the rule cannot drift from
    the SQL it is about. A query that is renamed but not changed reads the same tables; a
    query that is changed says so here without anybody updating a list.
    """
    path = contracts.root / "queries" / query
    if not path.is_file():  # pragma: no cover — the loader refuses a dangling query first
        return frozenset()
    return frozenset(tables_in(path.read_text(encoding="utf-8")))


def _bound(contract: DatapointContract) -> str:
    tolerance = contract.tolerance
    if tolerance.relative is not None:
        return f"{tolerance.relative:.4%}"
    return f"{tolerance.absolute} absolute"
