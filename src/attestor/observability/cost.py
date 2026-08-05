"""Cost per report and per tenant, measured rather than estimated.

On a multi-tenant platform the interesting number is not the monthly bill. It is **€ per
tenant** and **€ per report**, because those are the two figures that tell you whether a
customer is profitable and whether a change made things worse. A single total hides both.

Two design points worth stating.

**Every charge is attributed at the moment it is incurred**, to a session and therefore to a
tenant, rather than apportioned afterwards from a bill. Apportionment is a guess dressed as
accounting, and it always flatters whichever tenant nobody is looking at.

**Prices live in one table with a stated date.** They are list prices, they go stale, and the
table says so. A cost model that silently uses last year's prices is worse than one that
admits it needs updating, because the first is believed.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

#: The date these prices were taken. Verified against the calculator before any figure from
#: this module is quoted anywhere that matters.
PRICES_AS_OF = dt.date(2026, 8, 1)

#: Placeholder list prices in EUR. Deliberately obvious placeholders rather than plausible
#: invented numbers: a wrong number that looks right is quoted, and a placeholder is not.
PRICES: dict[str, Decimal] = {
    "model.input_1k_tokens": Decimal("0.0030"),
    "model.output_1k_tokens": Decimal("0.0150"),
    "embedding.1k_tokens": Decimal("0.0001"),
    "retrieval.query": Decimal("0.0004"),
    "athena.tb_scanned": Decimal("5.00"),
    "agentcore.runtime_second": Decimal("0.0001"),
    "agentcore.gateway_invocation": Decimal("0.00002"),
    "bda.page": Decimal("0.0100"),
}


class Meter(StrEnum):
    MODEL_INPUT = "model.input_1k_tokens"
    MODEL_OUTPUT = "model.output_1k_tokens"
    EMBEDDING = "embedding.1k_tokens"
    RETRIEVAL = "retrieval.query"
    ATHENA = "athena.tb_scanned"
    RUNTIME = "agentcore.runtime_second"
    GATEWAY = "agentcore.gateway_invocation"
    DOCUMENT_PARSE = "bda.page"


@dataclass(frozen=True, slots=True)
class Charge:
    meter: Meter
    quantity: Decimal
    tenant: str
    session_id: str
    #: What produced it. `resolve_datapoint`, `draft_narrative`, `ingest` — so a spike can be
    #: traced to a step rather than to a day.
    operation: str

    @property
    def amount(self) -> Decimal:
        return (PRICES[self.meter.value] * self.quantity).quantize(Decimal("0.000001"))


@dataclass(slots=True)
class CostMeter:
    charges: list[Charge] = field(default_factory=list)

    def record(
        self,
        meter: Meter,
        quantity: float | int | str | Decimal,
        *,
        tenant: str,
        session_id: str,
        operation: str,
    ) -> Charge:
        charge = Charge(
            meter=meter,
            quantity=Decimal(str(quantity)),
            tenant=tenant,
            session_id=session_id,
            operation=operation,
        )
        self.charges.append(charge)
        return charge

    @property
    def total(self) -> Decimal:
        return sum((charge.amount for charge in self.charges), Decimal(0))

    def by_tenant(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for charge in self.charges:
            totals[charge.tenant] += charge.amount
        return dict(sorted(totals.items()))

    def by_operation(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
        for charge in self.charges:
            totals[charge.operation] += charge.amount
        return dict(sorted(totals.items(), key=lambda item: -item[1]))

    def per_report(self, reports: int) -> Decimal:
        if reports <= 0:
            raise ValueError("cost per report needs at least one report")
        return (self.total / reports).quantize(Decimal("0.0001"))

    def report(self, *, reports: int = 0) -> str:
        lines = [
            f"total: EUR {self.total:.4f} over {len(self.charges)} charge(s) "
            f"(list prices as of {PRICES_AS_OF})",
            "",
            "per tenant:",
            *(f"  {tenant}: EUR {amount:.4f}" for tenant, amount in self.by_tenant().items()),
            "",
            "per operation:",
            *(
                f"  {operation}: EUR {amount:.4f}"
                for operation, amount in self.by_operation().items()
            ),
        ]
        if reports:
            lines += ["", f"per report: EUR {self.per_report(reports):.4f}"]
        return "\n".join(lines)
