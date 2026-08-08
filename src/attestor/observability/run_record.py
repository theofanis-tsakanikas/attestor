"""One record per report run — the thing you query afterwards to answer "what happened".

Without this, the outputs of a run are three Office files in a bucket and some CloudWatch
lines, and the questions people actually ask have no answer: *which datapoints blocked last
quarter, and for how long? which accepted defects are about to expire? what did this report
cost, and which step spent it?*

The record is written twice on purpose.

**As JSON, beside the artefacts.** Self-contained, diffable, and it travels with the report.
An auditor who is handed a folder gets the figures, their lineage and the reasons for every
omission without needing access to anything of ours.

**As rows, into Iceberg.** `gold.report_run` and `gold.report_datapoint`, queryable in
Athena. That is where trend questions live, and a trend across quarters is the one thing a
per-run JSON cannot answer.

Both are generated from the same object, so the dashboard, the warehouse and the folder
cannot disagree about what a run did.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from attestor.contracts.loader import ContractSet
from attestor.datapoints.resolver import Abstained, ResolutionSet, Resolved
from attestor.observability.cost import CostMeter

SCHEMA_VERSION = 1


class ArtefactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    artefact: str
    #: Digest of the bytes that were produced. An auditor re-reads a file, not a filename.
    sha256: str
    numerals_checked: int = 0
    provenance_clean: bool = True


class PublishedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    datapoint_id: str
    reference: str
    value: str | None
    unit: str | None
    lineage_id: str
    resolver_kind: str
    sources: tuple[str, ...] = ()


class OmissionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    datapoint_id: str
    reference: str
    reason_code: str
    outcome: str
    detail: str
    lawful: bool
    #: Present when an override accepted the defect. Who signed, and when it lapses — the two
    #: facts a reader needs and a log never has.
    approvers: tuple[str, ...] = ()
    override_expires_on: dt.date | None = None


class GateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    passed: bool
    detail: str = ""


class InjectionFinding(BaseModel):
    """A passage of the corpus that tried to instruct the model, and was reported instead.

    Not a blocker. The corpus is untrusted by construction, so an instruction found inside it
    is the control working rather than the report failing. It is still a finding about a
    document, and one an auditor is entitled to see: a company whose invoices carry
    instructions for the reporting system has a problem, whether or not the instructions
    worked this time.

    It exists because it used to not. The provider counted these into its usage dict, which
    is read for token counts and nothing else, so every observation was dropped one function
    after it was made — under a module docstring warning that exactly that turns a detected
    attack into an unreported one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    datapoint_id: str
    #: What the model said it saw. Never re-emitted into a prompt, and never rendered as
    #: instructions — it is quoted evidence of an attempt, in a field nothing executes.
    observation: str


class RunRecord(BaseModel):
    """Everything one report run did."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    run_id: str
    tenant: str
    tenant_name: str
    standard: str
    period: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None

    #: The headline. `False` means no artefact was written, and the blockers say why.
    issued: bool = False
    published: list[PublishedRecord] = Field(default_factory=list)
    limitations: list[OmissionRecord] = Field(default_factory=list)
    blockers: list[OmissionRecord] = Field(default_factory=list)
    artefacts: list[ArtefactRecord] = Field(default_factory=list)
    gates: list[GateRecord] = Field(default_factory=list)
    injection_findings: list[InjectionFinding] = Field(default_factory=list)
    cost_eur: str = "0.000000"
    cost_by_operation: dict[str, str] = Field(default_factory=dict)

    # ── Derived ──────────────────────────────────────────────────────────────

    @property
    def disclosure_rate(self) -> float:
        total = len(self.published) + len(self.limitations) + len(self.blockers)
        return len(self.published) / total if total else 0.0

    @property
    def expiring_acceptances(self) -> list[OmissionRecord]:
        return [o for o in self.limitations if o.override_expires_on is not None]

    # ── Persistence ──────────────────────────────────────────────────────────

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    def write(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.tenant}-{self.period}-{self.run_id}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load_all(cls, directory: Path | str) -> tuple[RunRecord, ...]:
        directory = Path(directory)
        if not directory.is_dir():
            return ()
        return tuple(
            cls.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        )

    # ── Warehouse rows ───────────────────────────────────────────────────────

    def run_row(self) -> dict[str, Any]:
        """One row for `gold.report_run`."""
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant,
            "standard": self.standard,
            "period": self.period,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "issued": self.issued,
            "published_count": len(self.published),
            "limitation_count": len(self.limitations),
            "injection_finding_count": len(self.injection_findings),
            "blocker_count": len(self.blockers),
            "artefact_count": len(self.artefacts),
            "cost_eur": self.cost_eur,
            "dq_status": "clean",
        }

    def datapoint_rows(self) -> list[dict[str, Any]]:
        """One row per datapoint for `gold.report_datapoint`.

        Published figures and omissions land in the *same* table, distinguished by a column.
        Splitting them would make "what did we not disclose, and why" a join, and the whole
        point of the omissions register is that it is as easy to read as the figures.
        """
        rows: list[dict[str, Any]] = []
        for entry in self.published:
            rows.append(
                {
                    "run_id": self.run_id,
                    "tenant_id": self.tenant,
                    "period": self.period,
                    "datapoint_id": entry.datapoint_id,
                    "reference": entry.reference,
                    "disclosed": True,
                    "value": entry.value,
                    "unit": entry.unit,
                    "lineage_id": entry.lineage_id,
                    "resolver_kind": entry.resolver_kind,
                    "reason_code": None,
                    "outcome": "published",
                    "dq_status": "clean",
                }
            )
        for entry in [*self.limitations, *self.blockers]:
            rows.append(
                {
                    "run_id": self.run_id,
                    "tenant_id": self.tenant,
                    "period": self.period,
                    "datapoint_id": entry.datapoint_id,
                    "reference": entry.reference,
                    "disclosed": False,
                    "value": None,
                    "unit": None,
                    "lineage_id": None,
                    "resolver_kind": None,
                    "reason_code": entry.reason_code,
                    "outcome": entry.outcome,
                    "dq_status": "clean",
                }
            )
        return rows


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(
    *,
    run_id: str,
    tenant: str,
    tenant_name: str,
    standard: str,
    period: str,
    started_at: dt.datetime,
    results: ResolutionSet,
    contracts: ContractSet,
    cost_meter: CostMeter | None = None,
) -> RunRecord:
    """Turn a resolution into a record. Artefacts and gates are attached by the caller."""
    record = RunRecord(
        run_id=run_id,
        tenant=tenant,
        tenant_name=tenant_name,
        standard=standard,
        period=period,
        started_at=started_at,
        issued=results.can_issue,
    )

    for outcome in sorted(results.published, key=lambda r: r.datapoint_id):
        assert isinstance(outcome, Resolved)
        contract = contracts[outcome.datapoint_id]
        record.published.append(
            PublishedRecord(
                datapoint_id=outcome.datapoint_id,
                reference=contract.reference,
                value=None if contract.is_model_authored else str(outcome.value),
                unit=outcome.unit,
                lineage_id=outcome.lineage.short_id,
                resolver_kind=outcome.lineage.resolver_kind,
                sources=tuple(
                    f"{s.table}@{s.snapshot_id}" if s.snapshot_id else s.table
                    for s in outcome.lineage.sources
                ),
            )
        )

        for observation in outcome.injection_observed:
            record.injection_findings.append(
                InjectionFinding(datapoint_id=outcome.datapoint_id, observation=observation)
            )

    for outcome in sorted(results.abstentions, key=lambda a: a.datapoint_id):
        entry = _omission(outcome, contracts)
        (record.blockers if outcome.blocks_report else record.limitations).append(entry)

    # What this one report cost, priced per meter and attributed per operation. It was always
    # measured — the resolver records a charge for every Athena scan and every model token —
    # and never carried anywhere, because nothing handed the resolver a meter to record into.
    # Every live run wrote `0.0000` after querying a real lakehouse.
    if cost_meter is not None:
        # Six places, not four. An Athena scan over a few megabytes is genuinely worth a
        # fraction of a cent, and rounding it to `0.0000` reports a run that queried a lakehouse
        # as having cost nothing. `Charge.amount` already quantizes here; the record should not
        # throw away what the meter kept.
        record.cost_eur = f"{cost_meter.total:.6f}"
        record.cost_by_operation = {
            operation: f"{amount:.6f}" for operation, amount in cost_meter.by_operation().items()
        }

    return record


def _omission(outcome: Abstained, contracts: ContractSet) -> OmissionRecord:
    contract = contracts[outcome.datapoint_id]
    override = outcome.override
    return OmissionRecord(
        datapoint_id=outcome.datapoint_id,
        reference=contract.reference,
        reason_code=outcome.reason_code,
        outcome=outcome.outcome.value,
        detail=outcome.detail,
        lawful=outcome.is_lawful,
        approvers=tuple(f"{a.approver} ({a.role})" for a in override.approvals) if override else (),
        override_expires_on=override.expires_on if override else None,
    )
