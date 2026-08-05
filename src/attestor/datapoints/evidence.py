"""Does the evidence exist that lets this figure be stated at all?

This runs *before* any query. A figure computed from data whose supporting documents are
missing is not a cheaper figure — it is an unsupported one, and an auditor will ask for the
invoice regardless of how clean the SQL was.

The index is scoped by tenant at every entry point and there is no unscoped read. That is
not defence in depth for its own sake: `evals/isolation/` probes this module directly, and a
filter that can be omitted is a filter that eventually is.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from attestor.contracts.model import DatapointContract

EVIDENCE_DIR = "evidence"


class EvidenceDocument(BaseModel):
    """One document in a tenant's corpus. Untrusted content; trusted metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=3)
    tenant: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    #: What kind of document this is, from the contract's vocabulary of evidence classes.
    document_class: str = Field(min_length=3)
    #: The period the document *covers*, which is not the same as when it was filed.
    covers_from: dt.date
    covers_to: dt.date
    #: Digest of the stored object. An auditor re-reads the same bytes, not "the file".
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_uri: str = Field(min_length=5)
    #: Set when the injection scanner found instruction-shaped content in this document.
    #: Such a document still counts as evidence — its *metadata* is trustworthy — but the
    #: narrative layer refuses to treat its text as anything but data.
    flagged_injection: bool = False

    @model_validator(mode="after")
    def _period_runs_forwards(self):
        if self.covers_to < self.covers_from:
            raise ValueError(f"{self.document_id}: covers_to precedes covers_from")
        return self

    def covers(self, *, start: dt.date, end: dt.date) -> bool:
        """True when the document overlaps the reporting period at all."""
        return self.covers_from <= end and self.covers_to >= start


class EvidenceIndex:
    """A tenant-scoped view of the evidence corpus.

    There is no constructor that yields an index over every tenant. `for_tenant` is the only
    way in, so a caller cannot forget to narrow.
    """

    def __init__(self, documents: Iterable[EvidenceDocument], *, tenant: str) -> None:
        self._tenant = tenant
        self._documents = tuple(d for d in documents if d.tenant == tenant)

    @classmethod
    def for_tenant(cls, root: Path | str, tenant: str) -> EvidenceIndex:
        root = Path(root)
        directory = root / EVIDENCE_DIR / tenant
        documents: list[EvidenceDocument] = []
        if directory.is_dir():
            for path in sorted(directory.rglob("*.yaml")):
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                for entry in payload.get("documents", []):
                    documents.append(EvidenceDocument.model_validate(entry))
        return cls(documents, tenant=tenant)

    @property
    def tenant(self) -> str:
        return self._tenant

    def __iter__(self) -> Iterator[EvidenceDocument]:
        return iter(self._documents)

    def __len__(self) -> int:
        return len(self._documents)

    def matching(
        self, *, classes: Iterable[str], start: dt.date, end: dt.date, in_period: bool
    ) -> tuple[EvidenceDocument, ...]:
        wanted = set(classes)
        found = tuple(d for d in self._documents if d.document_class in wanted)
        if in_period:
            found = tuple(d for d in found if d.covers(start=start, end=end))
        return found


class EvidenceVerdict(BaseModel):
    """What the corpus says about one datapoint, before any computation happens."""

    model_config = ConfigDict(frozen=True)

    satisfied: bool
    #: The reason code to abstain with, when not satisfied.
    reason_code: str | None = None
    documents: tuple[str, ...] = ()
    detail: str = ""


def check(
    contract: DatapointContract,
    index: EvidenceIndex,
    *,
    period_start: dt.date,
    period_end: dt.date,
) -> EvidenceVerdict:
    """Decide whether the corpus supports stating this datapoint.

    The three failure modes are kept apart on purpose, because they mean different things to
    a reader and carry different override rules:

    - nothing of the right class exists at all → `E_NO_EVIDENCE`
    - documents exist but none covers the period → `E_EVIDENCE_OUT_OF_PERIOD`
    - documents exist and cover the period, but not enough of them → `E_PARTIAL_BOUNDARY`
    """
    requirement = contract.evidence

    if requirement.inherited:
        return EvidenceVerdict(satisfied=True, detail="inherited from operands")
    if not requirement.required:
        return EvidenceVerdict(satisfied=True, detail="no evidence required")

    of_class = index.matching(
        classes=requirement.classes,
        start=period_start,
        end=period_end,
        in_period=False,
    )
    if not of_class:
        return EvidenceVerdict(
            satisfied=False,
            reason_code="E_NO_EVIDENCE",
            detail=(
                f"no document of class {{{', '.join(sorted(requirement.classes))}}} "
                f"exists for tenant {index.tenant}"
            ),
        )

    in_period = (
        index.matching(
            classes=requirement.classes,
            start=period_start,
            end=period_end,
            in_period=True,
        )
        if requirement.must_cover_period
        else of_class
    )
    if not in_period:
        return EvidenceVerdict(
            satisfied=False,
            reason_code="E_EVIDENCE_OUT_OF_PERIOD",
            detail=(
                f"{len(of_class)} document(s) of the required class exist, none covering "
                f"{period_start}..{period_end}"
            ),
        )

    demanded = requirement.documents_demanded
    if len(in_period) < demanded:
        return EvidenceVerdict(
            satisfied=False,
            reason_code="E_PARTIAL_BOUNDARY",
            documents=tuple(d.document_id for d in in_period),
            detail=f"{len(in_period)} document(s) in period, contract demands {demanded}",
        )

    return EvidenceVerdict(
        satisfied=True,
        documents=tuple(d.document_id for d in in_period),
        detail=f"{len(in_period)} document(s) in period",
    )
