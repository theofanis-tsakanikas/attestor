"""The closed vocabulary of reasons a datapoint may go undisclosed.

The single most important distinction in this module is between a **lawful omission**
and an **internal failure**.

A lawful omission is something the standard itself permits: the datapoint is not material
to this undertaking, it is inside a phase-in window, or disclosing it would be seriously
prejudicial. It is printed in the report, in the omissions register, and an auditor accepts
it as an answer.

An internal failure is *our* problem: a resolver crashed, source rows were quarantined, the
evidence is out of period. It is not an answer. Presenting it as one would launder a bug
into a regulatory exemption — which is exactly the failure mode this system exists to make
impossible.

So: an internal failure **blocks the report**. There is no configuration flag to soften that,
and `tests/contracts/test_reason_codes.py` fails if anyone adds one.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Disposition(StrEnum):
    """What an abstention reason means for the report as a whole."""

    #: The standard permits the omission. It is disclosed, registered, and assurable.
    LAWFUL_OMISSION = "lawful_omission"

    #: Our pipeline could not produce a trustworthy figure. The report does not ship.
    INTERNAL_FAILURE = "internal_failure"


@dataclass(frozen=True, slots=True)
class ReasonCode:
    """One permitted reason for not stating a figure."""

    code: str
    disposition: Disposition
    summary: str
    #: The sentence that appears in the report where the figure would have been.
    #: `{datapoint}` and `{reference}` are the only substitutions allowed — no free text
    #: reaches the page, so an LLM can never author an excuse.
    disclosure_template: str
    #: Which article of which standard permits this omission. Empty for internal failures:
    #: nothing permits shipping a broken number, which is the point.
    basis: str = ""

    @property
    def is_lawful(self) -> bool:
        return self.disposition is Disposition.LAWFUL_OMISSION

    @property
    def blocks_report(self) -> bool:
        return self.disposition is Disposition.INTERNAL_FAILURE


_CODES: tuple[ReasonCode, ...] = (
    # ── Lawful omissions ─────────────────────────────────────────────────────
    ReasonCode(
        code="E_NOT_MATERIAL",
        disposition=Disposition.LAWFUL_OMISSION,
        summary="The datapoint is not material for this undertaking.",
        disclosure_template=(
            "Not disclosed: {datapoint} ({reference}) was assessed as not material for the "
            "undertaking in the reporting period. The materiality assessment is described in "
            "the general disclosures."
        ),
        basis="ESRS 1 §29-33 (double materiality)",
    ),
    ReasonCode(
        code="E_PHASE_IN",
        disposition=Disposition.LAWFUL_OMISSION,
        summary="The standard permits phase-in of this datapoint for this undertaking.",
        disclosure_template=(
            "Not disclosed: {datapoint} ({reference}) falls within a transitional phase-in "
            "provision applicable to the undertaking for this reporting period."
        ),
        basis="ESRS 1 Appendix C (phase-in provisions)",
    ),
    ReasonCode(
        code="E_CONFIDENTIAL",
        disposition=Disposition.LAWFUL_OMISSION,
        summary="Disclosure would be seriously prejudicial to the undertaking.",
        disclosure_template=(
            "Not disclosed: {datapoint} ({reference}) is withheld because disclosure would be "
            "seriously prejudicial to the commercial position of the undertaking. The exemption "
            "and its scope are recorded in the omissions register."
        ),
        basis="ESRS 1 §35 (classified/sensitive information)",
    ),
    ReasonCode(
        code="E_NOT_APPLICABLE",
        disposition=Disposition.LAWFUL_OMISSION,
        summary="The datapoint does not apply to the undertaking's activities.",
        disclosure_template=(
            "Not disclosed: {datapoint} ({reference}) is not applicable to the activities of "
            "the undertaking in the reporting period."
        ),
        basis="ESRS 1 §34",
    ),
    # ── Internal failures ────────────────────────────────────────────────────
    ReasonCode(
        code="E_NO_EVIDENCE",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="No evidence document of a required class was found for the period.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) has no supporting evidence of a required class "
            "for the reporting period. This report cannot be issued."
        ),
    ),
    ReasonCode(
        code="E_PARTIAL_BOUNDARY",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="Evidence covers only part of the declared consolidation boundary.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) has evidence covering only part of the declared "
            "consolidation boundary. A partial figure would misstate the undertaking."
        ),
    ),
    ReasonCode(
        code="E_EVIDENCE_OUT_OF_PERIOD",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="The only available evidence falls outside the reporting period.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) is supported only by evidence outside the "
            "reporting period."
        ),
    ),
    ReasonCode(
        code="E_UPSTREAM_QUARANTINE",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="Source rows failed their data contract and were quarantined.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) depends on source records that failed their data "
            "contract and were quarantined. The figure is not computable from clean data."
        ),
    ),
    ReasonCode(
        code="E_METHOD_UNAVAILABLE",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="The declared methodology cannot be applied to the available data.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) cannot be computed under its declared "
            "methodology with the data available."
        ),
    ),
    ReasonCode(
        code="E_OUT_OF_TOLERANCE",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="Independent cross-checks of the figure disagree beyond declared tolerance.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) failed its own tolerance check — independent "
            "computations of the figure disagree beyond the declared bound."
        ),
    ),
    ReasonCode(
        code="E_RESOLVER_ERROR",
        disposition=Disposition.INTERNAL_FAILURE,
        summary="The deterministic resolver failed. No figure was produced.",
        disclosure_template=(
            "BLOCKED: {datapoint} ({reference}) could not be resolved. No figure was produced "
            "and none was inferred."
        ),
    ),
)

BY_CODE: dict[str, ReasonCode] = {rc.code: rc for rc in _CODES}

#: Codes that may legitimately appear in a shipped report.
LAWFUL_CODES: frozenset[str] = frozenset(rc.code for rc in _CODES if rc.is_lawful)

#: Codes that stop a report from being issued at all.
BLOCKING_CODES: frozenset[str] = frozenset(rc.code for rc in _CODES if rc.blocks_report)

ALL_CODES: frozenset[str] = frozenset(BY_CODE)


class UnknownReasonCode(ValueError):
    """Raised when a contract or a resolver names a reason outside the vocabulary.

    Deliberately fatal. A free-text reason is how "we could not compute it" becomes
    "it was not material" — the exact laundering this module prevents.
    """

    def __init__(self, code: str) -> None:
        super().__init__(
            f"unknown abstention reason {code!r}; "
            f"the vocabulary is closed: {', '.join(sorted(ALL_CODES))}"
        )
        self.code = code


def resolve(code: str) -> ReasonCode:
    """Look up a reason code, refusing anything outside the closed vocabulary."""
    try:
        return BY_CODE[code]
    except KeyError:
        raise UnknownReasonCode(code) from None


def render_disclosure(code: str, *, datapoint: str, reference: str) -> str:
    """Render the sentence that stands in for a figure that was not disclosed.

    Only the datapoint id and its standard reference are substituted. There is no path
    for model-authored prose to reach this sentence.
    """
    return resolve(code).disclosure_template.format(datapoint=datapoint, reference=reference)
