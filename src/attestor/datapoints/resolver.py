"""The only path from source data to a published figure.

Order of operations, and each step exists because skipping it produces a specific wrong
report:

1. **Evidence first.** Before any query runs. A figure computed over data whose supporting
   documents are missing is unsupported, however clean the SQL was.
2. **Resolve.** Constants come from their contract; SQL goes to the backend with bound
   parameters; derived figures are evaluated over canonical units; narratives are delegated
   and never produce a value.
3. **Cross-check.** Where a contract declares one, an independent computation must land
   inside the tolerance. Two source systems maintained by two teams have to agree.
4. **Decide the outcome.** A lawful omission is an answer. An internal failure consults the
   override register and, with nothing live, blocks.

Nothing in this module is allowed to invent a number. Every branch that cannot produce one
returns an abstention carrying a reason code from the closed vocabulary — including the
branch that catches an unexpected exception, which is precisely why `E_RESOLVER_ERROR`
exists and precisely why it has no override.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any

from attestor.contracts import derivation, overrides, reason_codes, units
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import DatapointContract
from attestor.contracts.overrides import Outcome, Override, OverrideRegister
from attestor.datapoints import evidence as evidence_module
from attestor.datapoints.backends import QueryBackend, QueryError, QueryResult, query_digest
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.lineage import LineageLedger, LineageRecord, SourceRef

#: Signature of the narrative provider. It receives the contract and the tenant context and
#: returns prose plus the citations behind it. It is injected, so the resolver has no
#: dependency on any model or SDK, and every offline test runs the real resolution path.
NarrativeProvider = Callable[[DatapointContract, "ResolutionContext"], "NarrativeDraft"]


@dataclass(frozen=True, slots=True)
class NarrativeDraft:
    text: str
    citations: tuple[str, ...]
    prompt_ref: str


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    """Who and when. Every resolution is scoped by these; there is no ambient tenant."""

    tenant: str
    period: str
    period_start: dt.date
    period_end: dt.date
    #: The date the outcome is judged as of — normally the report date, not today. Whether
    #: an override was live is a question about the moment the report was issued.
    as_of: dt.date
    #: Iceberg snapshot pins, per table. Empty means "current", and what current resolved to
    #: is recorded in the lineage so the run can be replayed.
    snapshots: dict[str, str] = field(default_factory=dict)
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Resolved:
    """A figure that may be published."""

    datapoint_id: str
    value: Decimal
    unit: str | None
    lineage: LineageRecord
    narrative: str | None = None
    citations: tuple[str, ...] = ()

    @property
    def is_published(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Abstained:
    """A figure that was not produced, and what happens to the report because of it."""

    datapoint_id: str
    reason_code: str
    outcome: Outcome
    detail: str
    override: Override | None = None
    lineage: LineageRecord | None = None

    @property
    def is_published(self) -> bool:
        return False

    @property
    def is_lawful(self) -> bool:
        return reason_codes.resolve(self.reason_code).is_lawful

    @property
    def blocks_report(self) -> bool:
        return self.outcome is Outcome.BLOCKED


Resolution = Resolved | Abstained


class ResolutionSet:
    """Every datapoint's outcome for one tenant and period."""

    def __init__(self, results: dict[str, Resolution], ledger: LineageLedger) -> None:
        self._results = results
        self.ledger = ledger

    def __iter__(self) -> Iterator[Resolution]:
        return iter(self._results.values())

    def __len__(self) -> int:
        return len(self._results)

    def __getitem__(self, datapoint_id: str) -> Resolution:
        return self._results[datapoint_id]

    def get(self, datapoint_id: str) -> Resolution | None:
        return self._results.get(datapoint_id)

    @property
    def published(self) -> tuple[Resolved, ...]:
        return tuple(r for r in self._results.values() if isinstance(r, Resolved))

    @property
    def abstentions(self) -> tuple[Abstained, ...]:
        return tuple(r for r in self._results.values() if isinstance(r, Abstained))

    @property
    def blockers(self) -> tuple[Abstained, ...]:
        """Anything that stops the report being issued. Non-empty means no report."""
        return tuple(a for a in self.abstentions if a.blocks_report)

    @property
    def can_issue(self) -> bool:
        return not self.blockers

    @property
    def limitations(self) -> tuple[Abstained, ...]:
        """Omissions and qualifications that must print on the face of the statement."""
        return tuple(a for a in self.abstentions if not a.blocks_report)


class Resolver:
    """Resolves a contract set for one tenant and period."""

    def __init__(
        self,
        *,
        contracts: ContractSet,
        backend: QueryBackend,
        evidence: EvidenceIndex,
        override_register: OverrideRegister,
        root: Path | str = ".",
        narrative_provider: NarrativeProvider | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if evidence.tenant is None:  # pragma: no cover — defensive
            raise ValueError("the evidence index must be tenant-scoped")
        self._contracts = contracts
        self._backend = backend
        self._evidence = evidence
        self._overrides = override_register
        self._root = Path(root)
        self._narrative = narrative_provider
        self._clock = clock

    # ── Entry point ──────────────────────────────────────────────────────────

    def resolve_all(self, context: ResolutionContext) -> ResolutionSet:
        if context.tenant != self._evidence.tenant:
            raise ValueError(
                f"resolver is scoped to tenant {self._evidence.tenant!r} "
                f"but was asked to resolve {context.tenant!r}"
            )
        results: dict[str, Resolution] = {}
        ledger = LineageLedger()
        for datapoint_id in self._contracts.resolution_order():
            contract = self._contracts[datapoint_id]
            results[datapoint_id] = self._resolve_one(contract, context, results, ledger)
        return ResolutionSet(results, ledger)

    # ── One datapoint ────────────────────────────────────────────────────────

    def _resolve_one(
        self,
        contract: DatapointContract,
        context: ResolutionContext,
        so_far: dict[str, Resolution],
        ledger: LineageLedger,
    ) -> Resolution:
        verdict = evidence_module.check(
            contract,
            self._evidence,
            period_start=context.period_start,
            period_end=context.period_end,
        )
        if not verdict.satisfied:
            return self._abstain(contract, context, verdict.reason_code, verdict.detail)

        try:
            match contract.resolver.kind:
                case "constant":
                    return self._resolve_constant(contract, context, ledger)
                case "sql":
                    return self._resolve_sql(contract, context, ledger)
                case "derived":
                    return self._resolve_derived(contract, context, so_far, ledger)
                case "narrative":
                    return self._resolve_narrative(contract, context, ledger)
        except _Abstain as abstention:
            return self._abstain(contract, context, abstention.reason_code, abstention.detail)
        except Exception as exc:
            # Anything unforeseen becomes E_RESOLVER_ERROR: the one failure nobody may sign
            # for, because nobody can judge the materiality of a figure nobody has seen.
            return self._abstain(
                contract,
                context,
                "E_RESOLVER_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
        raise AssertionError(
            f"unhandled resolver kind {contract.resolver.kind}"
        )  # pragma: no cover

    # ── Per resolver kind ────────────────────────────────────────────────────

    def _resolve_constant(
        self, contract: DatapointContract, context: ResolutionContext, ledger: LineageLedger
    ) -> Resolution:
        resolver = contract.resolver
        value = Decimal(str(resolver.value))
        record = ledger.record(
            LineageRecord(
                datapoint_id=contract.id,
                tenant=context.tenant,
                period=context.period,
                resolver_kind="constant",
                resolver_digest=query_digest(f"{resolver.value}|{resolver.source}"),
                resolver_ref=f"constant approved {resolver.approved_on} by {resolver.approved_by}",
                value=str(value),
                unit=contract.unit,
                computed_at=self._now(),
                run_id=context.run_id,
            )
        )
        return Resolved(contract.id, value, contract.unit, record)

    def _resolve_sql(
        self, contract: DatapointContract, context: ResolutionContext, ledger: LineageLedger
    ) -> Resolution:
        sql = self._read_query(contract.resolver.query)
        parameters = self._parameters(context)
        result = self._run(sql, parameters, context)

        if result.value is None:
            raise _Abstain(
                "E_NO_EVIDENCE",
                "the query ran and matched no rows; an empty result is not zero",
            )
        if result.quarantined_rows:
            raise _Abstain(
                "E_UPSTREAM_QUARANTINE",
                f"{result.quarantined_rows} source row(s) failed their data contract",
            )

        self._cross_check(contract, result.value, parameters, context)

        value = self._round(result.value, contract)
        record = ledger.record(
            LineageRecord(
                datapoint_id=contract.id,
                tenant=context.tenant,
                period=context.period,
                resolver_kind="sql",
                resolver_digest=query_digest(sql),
                resolver_ref=contract.resolver.query,
                parameters=parameters,
                sources=self._sources(result),
                value=str(value),
                unit=contract.unit,
                computed_at=self._now(),
                run_id=context.run_id,
            )
        )
        return Resolved(contract.id, value, contract.unit, record)

    def _resolve_derived(
        self,
        contract: DatapointContract,
        context: ResolutionContext,
        so_far: dict[str, Resolution],
        ledger: LineageLedger,
    ) -> Resolution:
        expression = contract.resolver.expression
        operands = sorted(derivation.referenced_datapoints(expression))

        # An operand that was not published propagates. A blocked operand blocks; a lawfully
        # omitted one omits, carrying the same reason code — which the loader has already
        # guaranteed this contract is allowed to state.
        for operand_id in operands:
            operand = so_far.get(operand_id)
            if operand is None or isinstance(operand, Abstained):
                reason = operand.reason_code if operand else "E_RESOLVER_ERROR"
                detail = (
                    f"operand {operand_id} was not published"
                    if operand
                    else f"operand {operand_id} was never resolved"
                )
                raise _Abstain(reason, detail)

        canonical: dict[str, Fraction] = {}
        for operand_id in operands:
            operand = so_far[operand_id]
            assert isinstance(operand, Resolved)
            canonical[operand_id] = self._to_canonical(operand.value, operand.unit)

        raw = derivation.evaluate(expression, canonical)
        value = self._from_canonical(raw, contract)

        record = ledger.record(
            LineageRecord(
                datapoint_id=contract.id,
                tenant=context.tenant,
                period=context.period,
                resolver_kind="derived",
                resolver_digest=query_digest(expression),
                resolver_ref=expression,
                inputs=tuple(ledger[operand_id].lineage_id for operand_id in operands),
                value=str(value),
                unit=contract.unit,
                computed_at=self._now(),
                run_id=context.run_id,
            )
        )
        return Resolved(contract.id, value, contract.unit, record)

    def _resolve_narrative(
        self, contract: DatapointContract, context: ResolutionContext, ledger: LineageLedger
    ) -> Resolution:
        if self._narrative is None:
            raise _Abstain(
                "E_METHOD_UNAVAILABLE",
                "no narrative provider is configured; prose is not invented here",
            )
        draft = self._narrative(contract, context)
        required = contract.resolver.grounding.min_citations
        if len(draft.citations) < required:
            raise _Abstain(
                "E_NO_EVIDENCE",
                f"narrative cited {len(draft.citations)} passage(s), contract demands {required}",
            )
        record = ledger.record(
            LineageRecord(
                datapoint_id=contract.id,
                tenant=context.tenant,
                period=context.period,
                resolver_kind="narrative",
                resolver_digest=query_digest(draft.prompt_ref),
                resolver_ref=contract.resolver.prompt_id,
                prompt_ref=draft.prompt_ref,
                computed_at=self._now(),
                run_id=context.run_id,
            )
        )
        return Resolved(
            contract.id,
            Decimal(0),
            None,
            record,
            narrative=draft.text,
            citations=draft.citations,
        )

    # ── Cross-check ──────────────────────────────────────────────────────────

    def _cross_check(
        self,
        contract: DatapointContract,
        primary: Decimal,
        parameters: dict[str, str],
        context: ResolutionContext,
    ) -> None:
        tolerance = contract.tolerance
        for query_path in tolerance.cross_check:
            alternative = self._run(self._read_query(query_path), parameters, context)
            if alternative.value is None:
                raise _Abstain(
                    "E_OUT_OF_TOLERANCE",
                    f"cross-check {query_path} matched no rows while the primary query did",
                )
            delta = abs(primary - alternative.value)
            if tolerance.absolute is not None and delta > Decimal(str(tolerance.absolute)):
                raise _Abstain(
                    "E_OUT_OF_TOLERANCE",
                    f"{query_path} differs by {delta} (bound {tolerance.absolute} absolute)",
                )
            if tolerance.relative is not None and primary != 0:
                relative = delta / abs(primary)
                if relative > Decimal(str(tolerance.relative)):
                    raise _Abstain(
                        "E_OUT_OF_TOLERANCE",
                        f"{query_path} differs by {relative:.4%} (bound {tolerance.relative:.4%})",
                    )

    # ── Abstention ───────────────────────────────────────────────────────────

    def _abstain(
        self,
        contract: DatapointContract,
        context: ResolutionContext,
        reason_code: str | None,
        detail: str,
    ) -> Abstained:
        code = reason_code or "E_RESOLVER_ERROR"
        resolved = reason_codes.resolve(code)

        if resolved.is_lawful:
            if code not in contract.abstention.allowed_reasons:
                # A lawful reason this contract never declared is not a lawful answer here.
                # It becomes an internal failure rather than being quietly accepted.
                return Abstained(
                    contract.id,
                    "E_METHOD_UNAVAILABLE",
                    Outcome.BLOCKED,
                    f"{code} is not among this contract's declared omissions ({detail})",
                )
            return Abstained(contract.id, code, Outcome.OMITTED_WITH_MATERIAL_LIMITATION, detail)

        outcome, override = overrides.decide(
            reason_code=code,
            tenant=context.tenant,
            datapoint_id=contract.id,
            period=context.period,
            register=self._overrides,
            as_of=context.as_of,
        )
        return Abstained(contract.id, code, outcome, detail, override=override)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _run(self, sql: str, parameters: dict[str, str], context: ResolutionContext) -> QueryResult:
        try:
            return self._backend.execute(
                sql=sql, parameters=parameters, snapshot_id=context.snapshots.get("*")
            )
        except QueryError as exc:
            raise _Abstain("E_RESOLVER_ERROR", str(exc)) from exc

    def _read_query(self, relative: str) -> str:
        path = self._root / "queries" / relative
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _Abstain("E_RESOLVER_ERROR", f"cannot read {path}: {exc}") from exc

    @staticmethod
    def _parameters(context: ResolutionContext) -> dict[str, str]:
        return {
            "tenant_id": context.tenant,
            "period_start": context.period_start.isoformat(),
            "period_end": context.period_end.isoformat(),
        }

    @staticmethod
    def _sources(result: QueryResult) -> tuple[SourceRef, ...]:
        return tuple(
            SourceRef(
                table=table,
                snapshot_id=result.snapshot_ids.get(table),
                rows=result.row_counts.get(table),
            )
            for table in result.tables
        )

    @staticmethod
    def _to_canonical(value: Decimal, unit: str | None) -> Fraction:
        exact = Fraction(value)
        return exact * units.resolve(unit).to_canonical if unit else exact

    @staticmethod
    def _from_canonical(value: Fraction, contract: DatapointContract) -> Decimal:
        if contract.unit:
            value = value / units.resolve(contract.unit).to_canonical
        return Resolver._round(Decimal(value.numerator) / Decimal(value.denominator), contract)

    @staticmethod
    def _round(value: Decimal, contract: DatapointContract) -> Decimal:
        if contract.precision is None:
            return value
        try:
            quantum = Decimal(1).scaleb(-contract.precision)
            return value.quantize(quantum, rounding=ROUND_HALF_EVEN)
        except InvalidOperation as exc:  # pragma: no cover — guards absurd magnitudes
            raise _Abstain(
                "E_RESOLVER_ERROR", f"cannot round to declared precision: {exc}"
            ) from exc

    def _now(self) -> dt.datetime:
        return self._clock() if self._clock else dt.datetime.now(dt.UTC)


class _Abstain(Exception):
    """Internal control flow: stop resolving this datapoint and abstain with a reason."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


def summarise(results: ResolutionSet) -> dict[str, Any]:
    """A compact, comparable snapshot of a run. Used by the reproducibility check."""
    return {
        "published": {r.datapoint_id: str(r.value) for r in results.published},
        "lineage": results.ledger.ids(),
        "abstentions": {
            a.datapoint_id: {"reason": a.reason_code, "outcome": a.outcome.value}
            for a in results.abstentions
        },
        "can_issue": results.can_issue,
    }
