"""Lineage — the record that lets an auditor walk a published figure back to a source row.

The design constraint that shapes everything here is claim 4: **re-resolving as of an earlier
instant must produce an identical lineage id**. So the id is a hash over exactly the inputs
that determine the value, and nothing else.

What is inside the hash: the datapoint, the tenant, the period, the resolver's identity
(query text, not just its path — a renamed file that changed content must not hash the same),
the bound parameters, the pinned snapshot, and the operand lineage ids for derived figures.

What is deliberately outside: `computed_at`, the run id, the model, the machine. Those are
recorded beside the hash because an auditor wants them, but a figure resolved twice from the
same data is the same figure, and a timestamp inside the hash would say otherwise.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

LINEAGE_VERSION = 1

ResolverKind = Literal["sql", "derived", "constant", "narrative"]


def _canonical(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace drift, stable float repr."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class SourceRef(BaseModel):
    """One table a figure was read from, pinned to the snapshot that was actually read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table: str
    #: Iceberg snapshot id. Recorded even when the query asked for "current", because
    #: "current" is not a thing an auditor can re-read a year later.
    snapshot_id: str | None = None
    #: Row count that satisfied the predicate. Not part of the hash — it is a diagnostic,
    #: and a legitimate late-arriving row would otherwise invalidate an unchanged figure.
    rows: int | None = None

    def identity(self) -> dict[str, Any]:
        return {"table": self.table, "snapshot_id": self.snapshot_id}


class LineageRecord(BaseModel):
    """Everything that determined one resolved figure, plus the context around it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = LINEAGE_VERSION
    datapoint_id: str
    tenant: str
    period: str
    resolver_kind: ResolverKind

    #: sha256 of the *text* of the query or expression that produced the value. A renamed
    #: file whose content changed must not hash the same as its predecessor.
    resolver_digest: str
    #: The query's path or the derived expression, kept readable for the annex.
    resolver_ref: str

    #: Bound parameters. Never a rendered SQL string — the whole point is that the tenant
    #: predicate was bound, not concatenated, and the record should show that.
    parameters: dict[str, str] = Field(default_factory=dict)
    sources: tuple[SourceRef, ...] = ()
    #: Lineage ids of the operands, for a derived figure. Order follows the expression.
    inputs: tuple[str, ...] = ()

    #: The value as published, in the contract's declared unit, as an exact decimal string.
    value: str | None = None
    unit: str | None = None

    # ── Context, recorded but outside the hash ───────────────────────────────
    computed_at: dt.datetime | None = None
    run_id: str | None = None
    #: For narrative datapoints: the prompt id and version that produced the text. A
    #: narrative has no value, so this is what an auditor inspects instead.
    prompt_ref: str | None = None

    @model_validator(mode="after")
    def _narratives_have_no_value(self) -> Self:
        if self.resolver_kind == "narrative" and self.value is not None:
            raise ValueError(
                f"{self.datapoint_id}: a narrative lineage record carries no value — "
                "model-authored text is never a figure"
            )
        if self.resolver_kind == "derived" and not self.inputs:
            raise ValueError(f"{self.datapoint_id}: a derived figure must record its operands")
        return self

    def identity(self) -> dict[str, Any]:
        """The inputs that determine the value. Changing any of these changes the figure."""
        return {
            "version": self.version,
            "datapoint_id": self.datapoint_id,
            "tenant": self.tenant,
            "period": self.period,
            "resolver_kind": self.resolver_kind,
            "resolver_digest": self.resolver_digest,
            "parameters": dict(sorted(self.parameters.items())),
            "sources": [source.identity() for source in self.sources],
            "inputs": list(self.inputs),
        }

    @property
    def lineage_id(self) -> str:
        """Deterministic over the identity. Two runs from the same data agree."""
        return digest(self.identity())

    @property
    def short_id(self) -> str:
        """What appears beside a figure in the auditor annex."""
        return self.lineage_id[:12]


class LineageLedger:
    """The lineage records produced by one resolution run, indexed by datapoint."""

    def __init__(self) -> None:
        self._records: dict[str, LineageRecord] = {}

    def record(self, entry: LineageRecord) -> LineageRecord:
        self._records[entry.datapoint_id] = entry
        return entry

    def __contains__(self, datapoint_id: object) -> bool:
        return datapoint_id in self._records

    def __getitem__(self, datapoint_id: str) -> LineageRecord:
        return self._records[datapoint_id]

    def __len__(self) -> int:
        return len(self._records)

    def get(self, datapoint_id: str) -> LineageRecord | None:
        return self._records.get(datapoint_id)

    def ids(self) -> dict[str, str]:
        """`{datapoint_id: lineage_id}` — the shape the reproducibility check compares."""
        return {key: record.lineage_id for key, record in sorted(self._records.items())}

    def as_annex(self) -> list[dict[str, Any]]:
        """The auditor annex rows: figure, where it came from, and how to re-read it."""
        rows: list[dict[str, Any]] = []
        for datapoint_id, record in sorted(self._records.items()):
            rows.append(
                {
                    "datapoint": datapoint_id,
                    "value": record.value,
                    "unit": record.unit,
                    "lineage": record.short_id,
                    "resolver": f"{record.resolver_kind}:{record.resolver_ref}",
                    "sources": [
                        f"{s.table}@{s.snapshot_id}" if s.snapshot_id else s.table
                        for s in record.sources
                    ],
                    "inputs": [self._short(i) for i in record.inputs],
                    "computed_at": record.computed_at.isoformat() if record.computed_at else None,
                }
            )
        return rows

    @staticmethod
    def _short(lineage_id: str) -> str:
        return lineage_id[:12]
