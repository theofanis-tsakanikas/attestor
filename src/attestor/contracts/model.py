"""The datapoint contract — the atomic unit of the system.

One YAML file per regulated figure. It declares what the figure means, which standard
demands it, how it is computed, what evidence must exist behind it, and which *lawful*
omissions the undertaking may claim if it cannot be stated.

Two invariants in this module carry most of the system's weight:

1. **A narrative resolver may only serve a narrative datapoint.** This is the boundary
   between the model and the numbers, expressed as a type error. There is no configuration
   under which an LLM produces something the report prints as a figure.

2. **A contract may only pre-authorize *lawful* omissions.** `abstention.allowed_reasons` is
   constrained to the lawful half of the reason vocabulary. A contract cannot declare in
   advance that a resolver crash is an acceptable answer, because it is not one.

Validation is split deliberately. This module enforces everything knowable from a single
contract in isolation. Anything that needs the whole set — a derived expression's operands,
a `supersedes` target, whether the SQL file exists — belongs to the loader.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from attestor.contracts import reason_codes, units

DATAPOINT_ID = r"^[A-Z][A-Z0-9]*(?:_[A-Za-z0-9.\-]+)+$"


class Standard(StrEnum):
    ESRS = "ESRS"
    EU_AI_ACT = "EU_AI_ACT"


class DatapointKind(StrEnum):
    QUANTITATIVE = "quantitative"
    NARRATIVE = "narrative"
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"


class Period(StrEnum):
    FISCAL_YEAR = "fiscal_year"
    POINT_IN_TIME = "point_in_time"
    NOT_TIME_BOUND = "not_time_bound"


class AssuranceLevel(StrEnum):
    LIMITED = "limited"
    REASONABLE = "reasonable"
    NONE = "none"


class ConsolidationBoundary(StrEnum):
    """Which entities a figure covers. Mixing these silently is a classic misstatement."""

    OPERATIONAL_CONTROL = "operational_control"
    FINANCIAL_CONTROL = "financial_control"
    EQUITY_SHARE = "equity_share"
    VALUE_CHAIN = "value_chain"
    NOT_APPLICABLE = "not_applicable"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


# ── Resolvers ────────────────────────────────────────────────────────────────


class SqlResolver(_Base):
    """A figure produced by one reviewed SQL statement over the lakehouse."""

    kind: Literal["sql"] = "sql"
    #: Path relative to `queries/`. The loader asserts the file exists.
    query: str = Field(pattern=r"^[a-z0-9_/]+\.sql$")
    returns: Literal["scalar", "table"] = "scalar"
    #: For `returns: table` — the column order the renderer will emit.
    columns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _columns_only_for_tables(self) -> Self:
        if self.returns == "scalar" and self.columns:
            raise ValueError("a scalar resolver cannot declare columns")
        if self.returns == "table" and not self.columns:
            raise ValueError("a table resolver must declare its column order")
        return self


class DerivedResolver(_Base):
    """A figure defined arithmetically over other datapoints."""

    kind: Literal["derived"] = "derived"
    expression: str = Field(min_length=1)


class ConstantResolver(_Base):
    """A reviewed constant — an emission factor, a conversion coefficient.

    Every field here exists so that a magic number in a regulated report carries a
    citation and a human's name. `source` alone is not enough: someone approved it.
    """

    kind: Literal["constant"] = "constant"
    value: float
    source: str = Field(min_length=8)
    approved_by: str = Field(min_length=2)
    approved_on: dt.date


class Grounding(_Base):
    """What a narrative must be anchored to before it may be printed."""

    #: Minimum number of distinct retrieved passages the narrative must cite.
    min_citations: int = Field(default=2, ge=1, le=10)
    #: Which corpus the citations must come from. `regulatory` is the standard itself;
    #: `evidence` is the tenant's own (untrusted) documents.
    corpus: Literal["regulatory", "evidence", "both"] = "both"
    #: Contextual-grounding threshold the guardrail is configured with, mirrored here so a
    #: contract and the deployed guardrail can be cross-checked. That cross-check is
    #: `scripts/check_guardrail_alignment.py`, and it runs in CI — for a while this comment
    #: claimed a check that did not exist, which is a worse state than having no comment.
    threshold: float = Field(default=0.75, ge=0.0, le=1.0)


class NarrativeResolver(_Base):
    """Prose authored by the model, grounded and gated. It never produces a figure."""

    kind: Literal["narrative"] = "narrative"
    #: Identifier of a versioned prompt. Prompts live in `prompts/`, never inline.
    prompt_id: str = Field(pattern=r"^[a-z0-9_.\-]+$")
    grounding: Grounding = Grounding()
    #: Hard ceiling on generated length. A narrative that runs long is a narrative that
    #: started reasoning about numbers.
    max_words: int = Field(default=250, ge=20, le=2000)


Resolver = Annotated[
    SqlResolver | DerivedResolver | ConstantResolver | NarrativeResolver,
    Field(discriminator="kind"),
]


# ── Supporting declarations ──────────────────────────────────────────────────


class EvidenceRequirement(_Base):
    """What must exist in the tenant's corpus before a figure may be stated.

    There are three honest states, not two:

    - **required** — documents of the named classes must exist for the period.
    - **inherited** — the datapoint is derived, so it has no evidence of its own; its
      operands carry it, and a gap in any operand propagates up. Saying `required: false`
      here would be a lie, and inventing a document class called
      "derived_from_disclosed_components" would be a worse one.
    - **not required** — genuinely optional. Rare, and it disqualifies the datapoint from
      assurance.
    """

    required: bool = True
    #: The datapoint is derived; its evidence obligation is its operands'.
    inherited: bool = False
    #: Document classes that satisfy the requirement, e.g. `utility_invoice`.
    classes: tuple[str, ...] = ()
    #: Unset means "one, the obvious minimum". Stating it is how a contract demands twelve
    #: monthly invoices rather than accepting a single annual estimate.
    min_documents: int | None = Field(default=None, ge=0)
    #: Whether the evidence must fall inside the reporting period.
    must_cover_period: bool = True

    @model_validator(mode="after")
    def _states_are_mutually_exclusive(self) -> Self:
        if self.inherited:
            if self.required:
                raise ValueError("inherited evidence cannot also be directly required")
            if self.classes or self.min_documents is not None:
                raise ValueError(
                    "inherited evidence names no document classes of its own — "
                    "the obligation belongs to the operands"
                )
            return self
        if self.required:
            if not self.classes:
                raise ValueError("required evidence must name at least one document class")
            if self.min_documents is not None and self.min_documents < 1:
                raise ValueError("required evidence must demand at least one document")
        return self

    @property
    def documents_demanded(self) -> int:
        """How many documents must actually be present for this datapoint to be stated."""
        if self.min_documents is not None:
            return self.min_documents
        return 1 if self.required else 0

    @property
    def is_satisfiable_by_documents(self) -> bool:
        """True when this datapoint's own corpus can settle the question."""
        return self.required and not self.inherited


class Tolerance(_Base):
    """How far independent computations of the same figure may disagree.

    `cross_check` names alternative queries that must land within the bound. A figure with
    no cross-check is a figure nobody has ever verified — permitted, but visible.
    """

    relative: float | None = Field(default=None, gt=0, le=0.5)
    absolute: float | None = Field(default=None, gt=0)
    cross_check: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _at_least_one_bound(self) -> Self:
        if self.cross_check and self.relative is None and self.absolute is None:
            raise ValueError("a cross-check needs a relative or absolute bound to check against")
        return self


class Abstention(_Base):
    """Which *lawful* omissions this datapoint may claim.

    Internal failures are never declared here. They are always possible, they always block
    the report, and no contract gets to pre-authorize one.
    """

    allowed_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _only_lawful_reasons(self) -> Self:
        for code in self.allowed_reasons:
            resolved = reason_codes.resolve(code)  # raises UnknownReasonCode
            if not resolved.is_lawful:
                raise ValueError(
                    f"{code} is an internal failure and cannot be pre-authorized by a contract; "
                    "only lawful omissions may be declared"
                )
        if len(set(self.allowed_reasons)) != len(self.allowed_reasons):
            raise ValueError("duplicate abstention reason")
        return self


class Assurance(_Base):
    level: AssuranceLevel = AssuranceLevel.LIMITED
    #: Whether this datapoint appears in the auditor annex with its full lineage.
    auditor_annex: bool = True


class Supersedes(_Base):
    """A restatement. Changing a unit, boundary or methodology is not a silent edit."""

    contract_version: int = Field(ge=1)
    effective_from: dt.date
    reason: str = Field(min_length=20)


# ── The contract ─────────────────────────────────────────────────────────────


class DatapointContract(_Base):
    """One regulated figure, fully declared."""

    id: str = Field(pattern=DATAPOINT_ID)
    version: int = Field(default=1, ge=1)
    standard: Standard
    standard_version: str = Field(min_length=4)
    #: The exact clause that demands this datapoint, quoted in the auditor annex.
    reference: str = Field(min_length=3)
    title: str = Field(min_length=3)
    description: str = Field(default="", max_length=2000)

    kind: DatapointKind
    unit: str | None = None
    #: Decimal places the rendered figure carries. Required for quantitative datapoints:
    #: rounding is a disclosure decision, not a formatting preference.
    precision: int | None = Field(default=None, ge=0, le=6)
    period: Period = Period.FISCAL_YEAR
    boundary: ConsolidationBoundary = ConsolidationBoundary.NOT_APPLICABLE
    methodology: str = ""

    resolver: Resolver
    evidence: EvidenceRequirement = EvidenceRequirement(required=True, classes=("declaration",))
    tolerance: Tolerance = Tolerance()
    abstention: Abstention = Abstention()
    assurance: Assurance = Assurance()
    supersedes: Supersedes | None = None
    tags: tuple[str, ...] = ()

    # ── Invariants ───────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _narrative_resolvers_serve_narrative_datapoints_only(self) -> Self:
        """The boundary between the model and the numbers, as a type error."""
        is_narrative_resolver = self.resolver.kind == "narrative"
        is_narrative_datapoint = self.kind is DatapointKind.NARRATIVE
        if is_narrative_resolver and not is_narrative_datapoint:
            raise ValueError(
                f"{self.id}: a narrative resolver cannot serve a {self.kind.value} datapoint — "
                "model-authored text must never become a figure"
            )
        if is_narrative_datapoint and not is_narrative_resolver:
            raise ValueError(
                f"{self.id}: a narrative datapoint must use a narrative resolver "
                f"(got {self.resolver.kind})"
            )
        return self

    @model_validator(mode="after")
    def _quantitative_datapoints_are_fully_specified(self) -> Self:
        if self.kind is DatapointKind.QUANTITATIVE:
            if self.unit is None:
                raise ValueError(f"{self.id}: a quantitative datapoint must declare a unit")
            units.resolve(self.unit)  # raises UnknownUnit
            if self.precision is None:
                raise ValueError(
                    f"{self.id}: a quantitative datapoint must declare precision — "
                    "rounding is a disclosure decision"
                )
        elif self.unit is not None:
            raise ValueError(f"{self.id}: only quantitative datapoints carry a unit")
        elif self.precision is not None:
            raise ValueError(f"{self.id}: only quantitative datapoints carry precision")
        return self

    @model_validator(mode="after")
    def _emissions_figures_declare_a_boundary(self) -> Self:
        """A tonne of CO2e without a consolidation boundary is not a disclosure."""
        if (
            self.kind is DatapointKind.QUANTITATIVE
            and self.unit in {"tCO2e", "ktCO2e", "kgCO2e"}
            and self.boundary is ConsolidationBoundary.NOT_APPLICABLE
        ):
            raise ValueError(
                f"{self.id}: a greenhouse-gas figure must declare its consolidation boundary"
            )
        return self

    @model_validator(mode="after")
    def _restatements_are_explicit(self) -> Self:
        if self.version > 1 and self.supersedes is None:
            raise ValueError(
                f"{self.id}: version {self.version} without a `supersedes` block — "
                "a changed contract is a restatement and must say so"
            )
        if self.supersedes and self.supersedes.contract_version >= self.version:
            raise ValueError(
                f"{self.id}: supersedes version {self.supersedes.contract_version} "
                f"is not older than {self.version}"
            )
        return self

    @model_validator(mode="after")
    def _derived_datapoints_inherit_their_evidence(self) -> Self:
        """A derived figure has no evidence of its own, and must not pretend otherwise."""
        is_derived = self.resolver.kind == "derived"
        if is_derived and not self.evidence.inherited:
            raise ValueError(
                f"{self.id}: a derived datapoint inherits evidence from its operands — "
                "declare `evidence.inherited: true` rather than inventing a document class"
            )
        if self.evidence.inherited and not is_derived:
            raise ValueError(
                f"{self.id}: only a derived datapoint can inherit evidence "
                f"(resolver is {self.resolver.kind})"
            )
        return self

    @model_validator(mode="after")
    def _assured_datapoints_keep_their_evidence(self) -> Self:
        if (
            self.assurance.level is not AssuranceLevel.NONE
            and not self.evidence.required
            and not self.evidence.inherited
        ):
            raise ValueError(
                f"{self.id}: an assured datapoint cannot waive its evidence requirement"
            )
        return self

    # ── Convenience ──────────────────────────────────────────────────────────

    @property
    def is_quantitative(self) -> bool:
        return self.kind is DatapointKind.QUANTITATIVE

    @property
    def is_model_authored(self) -> bool:
        """True when a language model writes this datapoint's content."""
        return self.resolver.kind == "narrative"

    @property
    def dimension(self) -> units.Dimension:
        return units.resolve(self.unit).dimension if self.unit else {}
