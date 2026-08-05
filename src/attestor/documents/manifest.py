"""The render manifest — what the provenance gate checks a finished document against.

A document is assembled from runs of text, and every run knows where it came from. That
provenance is the whole basis of claim 3, so the taxonomy is small and the rule attached to
each kind is absolute:

===============  ==========================================  =====================
kind             origin                                      numerals permitted?
===============  ==========================================  =====================
``STATIC``       committed template text, reviewed in a PR    yes
``FIGURE``       a resolved datapoint, carrying its lineage   yes — it *is* the figure
``META``         deterministic context: tenant, period, date  yes
``LIMITATION``   generated from the override register         yes
``CITATION``     a retrieval identifier the model quoted      yes — it is an id
``NARRATIVE``    written by a language model                  **no**
===============  ==========================================  =====================

The last row is the point. Everything else on the page can be traced to a contract, a
commit, or a signature. A narrative run is the only text a model authored, so it is the only
text where a digit would mean a model produced a number — and there is no threshold, no
tolerance and no allowlist for that.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: A numeral token: digits, optionally grouped by thousands separators and a decimal
#: mark.
#:
#: Deliberately *not* greedy across whitespace. An earlier version was, and it fused
#: adjacent table cells into tokens like "53   467.6" that matched nothing in the
#: manifest — so the gate failed on a perfectly clean document. A control that cries
#: wolf on correct input is a control somebody switches off.
NUMERAL = re.compile(r"\d+(?:[.,]\d+)*")


class RunKind(StrEnum):
    STATIC = "static"
    FIGURE = "figure"
    META = "meta"
    LIMITATION = "limitation"
    #: A retrieval identifier such as ``[ev:7f3a]``. It sits inside model-authored prose but
    #: is not prose: the model chose *which* passage to cite, and the identifier itself comes
    #: from the retriever. Splitting it out is what lets the narrative rule stay absolute —
    #: otherwise every citation marker would read as a model writing digits.
    CITATION = "citation"
    NARRATIVE = "narrative"

    @property
    def permits_numerals(self) -> bool:
        return self is not RunKind.NARRATIVE


class NumeralInNarrative(ValueError):
    """A model-authored run contains a digit. The build stops here, not at review."""

    def __init__(self, datapoint_id: str, found: list[str], excerpt: str) -> None:
        super().__init__(
            f"narrative for {datapoint_id} contains numeral(s) {found}: "
            f"...{excerpt}... — figures are placed by the resolver, never written by a model"
        )
        self.datapoint_id = datapoint_id
        self.found = found


def numerals(text: str) -> list[str]:
    """Every numeral token in a string, normalised of surrounding whitespace."""
    return [match.group(0).strip() for match in NUMERAL.finditer(text)]


class TextRun(BaseModel):
    """One contiguous piece of a document, and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RunKind
    text: str
    #: Present for FIGURE and NARRATIVE runs.
    datapoint_id: str | None = None
    #: Present for FIGURE runs: the lineage id whose value this text renders.
    lineage_id: str | None = None
    #: Where in the document this run lives, for the failure message.
    location: str = ""

    @property
    def numerals(self) -> list[str]:
        return numerals(self.text)


class RenderManifest(BaseModel):
    """Every run in one rendered artefact, plus the context that produced it."""

    model_config = ConfigDict(extra="forbid")

    tenant: str
    period: str
    template_id: str
    artefact: str
    runs: list[TextRun] = Field(default_factory=list)

    def add(self, run: TextRun) -> TextRun:
        """Append a run, refusing a narrative that carries a digit.

        The refusal happens here rather than in the gate so the failure lands at the moment
        the offending text was produced, naming the datapoint. The gate re-checks the
        finished file anyway — a manifest is a claim about a document, and claim 3 is only
        worth anything if the document itself is inspected.
        """
        if not run.kind.permits_numerals:
            found = run.numerals
            if found:
                raise NumeralInNarrative(run.datapoint_id or "?", found, run.text[:120])
        self.runs.append(run)
        return run

    def __iter__(self) -> Iterator[TextRun]:  # type: ignore[override]
        return iter(self.runs)

    def of_kind(self, *kinds: RunKind) -> tuple[TextRun, ...]:
        wanted = set(kinds)
        return tuple(run for run in self.runs if run.kind in wanted)

    @property
    def figures(self) -> tuple[TextRun, ...]:
        return self.of_kind(RunKind.FIGURE)

    def permitted_numerals(self) -> Counter[str]:
        """The multiset of numeral tokens this document is allowed to contain."""
        counter: Counter[str] = Counter()
        for run in self.runs:
            if run.kind.permits_numerals:
                counter.update(run.numerals)
        return counter

    def narrative_text(self) -> tuple[str, ...]:
        return tuple(run.text for run in self.of_kind(RunKind.NARRATIVE))

    def lineage_for(self, datapoint_id: str) -> str | None:
        for run in self.figures:
            if run.datapoint_id == datapoint_id:
                return run.lineage_id
        return None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def merge_numerals(manifests: Iterable[RenderManifest]) -> Counter[str]:
    total: Counter[str] = Counter()
    for manifest in manifests:
        total.update(manifest.permitted_numerals())
    return total
