"""The recorded key to a closed door. See `docs/adr/0001-fail-closed-with-a-recorded-key.md`.

An internal failure blocks the report. This module is the only way that block is ever
lifted, and everything about it is designed so that lifting it leaves more evidence than
working around it would.

The three things that make this a control rather than a rubber stamp:

- **No automated principal may sign.** Approvers are checked against patterns that catch
  roles, service accounts, bots and ARNs. The system cannot open a door for itself, and an
  agent cannot be talked into opening one either.
- **An override never produces a lawful omission.** It changes whether the build stops. It
  never changes what the defect *is*. `E_UPSTREAM_QUARANTINE` stays
  `E_UPSTREAM_QUARANTINE` in the record and on the page.
- **`E_RESOLVER_ERROR` has no key.** A crashed resolver is an unknown deficiency, so nobody
  — including the approver — has the information the approval would be about.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from attestor.contracts import reason_codes

OVERRIDES_DIR = "overrides"


class OverrideEffect(StrEnum):
    """What an override causes to happen. Neither value makes the defect go away."""

    #: The figure is published with the defect disclosed beside it. Only where a figure
    #: actually exists — which, among internal failures, is `E_OUT_OF_TOLERANCE` alone.
    PUBLISH_WITH_QUALIFICATION = "publish_with_qualification"

    #: The report is issued; this datapoint is not. The statement carries it as a material
    #: limitation, on its face, where the auditor reads.
    OMIT_WITH_MATERIAL_LIMITATION = "omit_with_material_limitation"


class Outcome(StrEnum):
    """What happens to a datapoint that failed, after the register has been consulted."""

    BLOCKED = "blocked"
    PUBLISHED_WITH_QUALIFICATION = "published_with_qualification"
    OMITTED_WITH_MATERIAL_LIMITATION = "omitted_with_material_limitation"


class OverrideRule(BaseModel):
    """Who may turn which key, for how long, to what effect."""

    model_config = ConfigDict(frozen=True)

    code: str
    #: Empty means the door has no key. `E_RESOLVER_ERROR` is the only such door.
    permitted_effects: frozenset[OverrideEffect] = frozenset()
    approvals_required: int = 1
    approver_roles: frozenset[str] = frozenset()
    max_duration_days: int = 90

    @property
    def is_overridable(self) -> bool:
        return bool(self.permitted_effects)


#: Roles permitted to approve. A closed list, because "who signed for this" is the first
#: question an auditor asks and a free-text job title is not an answer.
APPROVER_ROLES: frozenset[str] = frozenset(
    {
        "head_of_sustainability_reporting",
        "chief_financial_officer",
        "data_protection_officer",
        "audit_committee_chair",
        "engagement_partner",
    }
)

_SENIOR = frozenset({"chief_financial_officer", "audit_committee_chair", "engagement_partner"})

_RULES: tuple[OverrideRule, ...] = (
    # A figure exists; two computations of it disagree. A human can weigh that, so this is
    # the one internal failure that may still be published — with the discrepancy disclosed
    # beside it, and two signatures behind it.
    OverrideRule(
        code="E_OUT_OF_TOLERANCE",
        permitted_effects=frozenset(
            {
                OverrideEffect.PUBLISH_WITH_QUALIFICATION,
                OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION,
            }
        ),
        approvals_required=2,
        approver_roles=APPROVER_ROLES,
        max_duration_days=60,
    ),
    # Known deficiencies: we know exactly what is missing. They may be signed off, but only
    # into an omission the report declares — never into a published figure, because there
    # is no figure to publish.
    OverrideRule(
        code="E_NO_EVIDENCE",
        permitted_effects=frozenset({OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION}),
        approvals_required=2,
        approver_roles=APPROVER_ROLES,
        max_duration_days=90,
    ),
    OverrideRule(
        code="E_PARTIAL_BOUNDARY",
        permitted_effects=frozenset({OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION}),
        approvals_required=2,
        approver_roles=APPROVER_ROLES,
        max_duration_days=90,
    ),
    OverrideRule(
        code="E_EVIDENCE_OUT_OF_PERIOD",
        permitted_effects=frozenset({OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION}),
        approvals_required=1,
        approver_roles=APPROVER_ROLES,
        max_duration_days=90,
    ),
    OverrideRule(
        code="E_UPSTREAM_QUARANTINE",
        permitted_effects=frozenset({OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION}),
        approvals_required=1,
        approver_roles=APPROVER_ROLES,
        max_duration_days=90,
    ),
    OverrideRule(
        code="E_METHOD_UNAVAILABLE",
        permitted_effects=frozenset({OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION}),
        approvals_required=2,
        approver_roles=_SENIOR,
        max_duration_days=180,
    ),
    # The door with no key. A crashed resolver is an *unknown* deficiency: nobody can judge
    # the materiality of a figure nobody has seen, so an approval here would be a signature
    # on an empty page. Fix the resolver.
    OverrideRule(code="E_RESOLVER_ERROR", permitted_effects=frozenset()),
)

RULES: dict[str, OverrideRule] = {rule.code: rule for rule in _RULES}

#: Patterns that mark a principal as non-human. An override signed by a role, a service
#: account or an agent is not a human decision, whatever the YAML says.
_NON_HUMAN = re.compile(
    r"(^arn:)|(\brole\b)|(^svc[-_])|(-sa$)|(\bservice[-_ ]account\b)|(\bbot\b)"
    r"|(\bagent\b)|(\bautomation\b)|(\bsystem\b)|(\bci\b)|(\battestor\b)|(\[bot\])",
    re.IGNORECASE,
)


class NotOverridable(ValueError):
    """Someone tried to sign for a door that has no key."""


def ensure_overridable(reason_code: str, effect: OverrideEffect) -> OverrideRule:
    """Assert that this failure may be overridden to this effect, and return the rule.

    Exposed separately from model validation because callers need to ask the question
    without first constructing an `Override` — the resolver asks it to decide whether to
    even offer a break-glass path, and pydantic would wrap the exception where it is raised
    inside a validator.
    """
    rule = RULES.get(reason_code)
    if rule is None:
        raise ValueError(f"no override rule defined for {reason_code}")
    if not rule.is_overridable:
        raise NotOverridable(
            f"{reason_code} cannot be overridden by anyone, for any duration. "
            "A crashed resolver is an unknown deficiency: nobody can judge the "
            "materiality of a figure nobody has seen. Fix the resolver."
        )
    if effect not in rule.permitted_effects:
        permitted = ", ".join(sorted(e.value for e in rule.permitted_effects))
        raise ValueError(
            f"{reason_code} may not be overridden to {effect.value}; permitted: {permitted}"
        )
    return rule


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approver: str = Field(min_length=3)
    role: str
    approved_on: dt.date
    #: Where the signature actually lives — minute number, ticket, DPIA reference. The YAML
    #: is a record *of* an approval, not the approval itself.
    evidence_reference: str = Field(min_length=4)

    @model_validator(mode="after")
    def _approver_is_a_person(self) -> Self:
        if _NON_HUMAN.search(self.approver):
            raise ValueError(
                f"{self.approver!r} looks like an automated principal; "
                "only a named human may approve an override"
            )
        if self.role not in APPROVER_ROLES:
            raise ValueError(
                f"unknown approver role {self.role!r}; permitted roles are "
                f"{', '.join(sorted(APPROVER_ROLES))}"
            )
        return self


class Override(BaseModel):
    """One signed, expiring decision to issue a report despite a known defect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    datapoint_id: str
    #: The reporting period this applies to. An override is never open-ended across periods.
    period: str = Field(pattern=r"^\d{4}(-Q[1-4])?$")
    reason_code: str
    effect: OverrideEffect
    justification: str = Field(min_length=60)
    requested_by: str = Field(min_length=3)
    approvals: tuple[Approval, ...] = Field(min_length=1)
    expires_on: dt.date

    @model_validator(mode="after")
    def _only_internal_failures_need_overriding(self) -> Self:
        resolved = reason_codes.resolve(self.reason_code)  # raises on unknown codes
        if resolved.is_lawful:
            raise ValueError(
                f"{self.reason_code} is a lawful omission — it is already an acceptable "
                "answer and needs no override"
            )
        return self

    @model_validator(mode="after")
    def _the_door_must_have_a_key(self) -> Self:
        ensure_overridable(self.reason_code, self.effect)
        return self

    @model_validator(mode="after")
    def _enough_distinct_signatures(self) -> Self:
        rule = RULES[self.reason_code]
        approvers = [approval.approver.strip().casefold() for approval in self.approvals]
        if len(set(approvers)) != len(approvers):
            raise ValueError("the same person cannot sign an override twice")
        if len(approvers) < rule.approvals_required:
            raise ValueError(
                f"{self.reason_code} requires {rule.approvals_required} approval(s), "
                f"got {len(approvers)}"
            )
        if self.requested_by.strip().casefold() in approvers:
            raise ValueError(
                f"{self.requested_by!r} requested this override and cannot also approve it"
            )
        allowed = rule.approver_roles or APPROVER_ROLES
        for approval in self.approvals:
            if approval.role not in allowed:
                raise ValueError(
                    f"{approval.role!r} may not approve {self.reason_code}; "
                    f"permitted roles: {', '.join(sorted(allowed))}"
                )
        return self

    @model_validator(mode="after")
    def _expiry_is_bounded(self) -> Self:
        rule = RULES[self.reason_code]
        signed_on = max(approval.approved_on for approval in self.approvals)
        if self.expires_on <= signed_on:
            raise ValueError("an override that expires on or before it was signed is not one")
        span = (self.expires_on - signed_on).days
        if span > rule.max_duration_days:
            raise ValueError(
                f"{self.reason_code} overrides last at most {rule.max_duration_days} days; "
                f"this one runs {span}"
            )
        return self

    # ── Behaviour ────────────────────────────────────────────────────────────

    def is_live(self, as_of: dt.date) -> bool:
        return as_of <= self.expires_on

    def applies_to(self, *, tenant: str, datapoint_id: str, period: str, reason_code: str) -> bool:
        return (
            self.tenant == tenant
            and self.datapoint_id == datapoint_id
            and self.period == period
            and self.reason_code == reason_code
        )

    @property
    def outcome(self) -> Outcome:
        return (
            Outcome.PUBLISHED_WITH_QUALIFICATION
            if self.effect is OverrideEffect.PUBLISH_WITH_QUALIFICATION
            else Outcome.OMITTED_WITH_MATERIAL_LIMITATION
        )

    def render_limitation(self, *, reference: str) -> str:
        """The sentence this override puts on the face of the statement.

        Note what it does not do: it does not soften the reason code, and it names the
        approvers. A reader learns that a defect was accepted, by whom, and until when.
        """
        signatories = ", ".join(f"{a.approver} ({a.role})" for a in self.approvals)
        defect = reason_codes.resolve(self.reason_code).summary
        if self.effect is OverrideEffect.PUBLISH_WITH_QUALIFICATION:
            lead = (
                f"Qualified disclosure: {self.datapoint_id} ({reference}) is disclosed "
                f"notwithstanding a known defect."
            )
        else:
            lead = (
                f"Material limitation: {self.datapoint_id} ({reference}) is not disclosed "
                f"for the period."
            )
        return (
            f"{lead} {defect} Accepted under a recorded override "
            f"({self.reason_code}) approved by {signatories}, expiring {self.expires_on}."
        )


class OverrideRegister:
    """Every override in the repository, indexed and queryable as of a date."""

    def __init__(self, overrides: tuple[Override, ...], sources: dict[int, Path]) -> None:
        self._overrides = overrides
        self._sources = sources

    def __iter__(self) -> Iterator[Override]:
        return iter(self._overrides)

    def __len__(self) -> int:
        return len(self._overrides)

    def source_of(self, override: Override) -> Path | None:
        return self._sources.get(id(override))

    def find(
        self,
        *,
        tenant: str,
        datapoint_id: str,
        period: str,
        reason_code: str,
        as_of: dt.date,
    ) -> Override | None:
        """The live override for this exact failure, or `None`.

        Exactness matters: an override signed for Scope 3 in 2026 does not cover Scope 1,
        and does not cover 2027. Nothing here is fuzzy.
        """
        for override in self._overrides:
            if override.applies_to(
                tenant=tenant,
                datapoint_id=datapoint_id,
                period=period,
                reason_code=reason_code,
            ) and override.is_live(as_of):
                return override
        return None

    def expired(self, as_of: dt.date) -> tuple[Override, ...]:
        """Overrides that have lapsed. CI turns red on a non-empty result — by design."""
        return tuple(o for o in self._overrides if not o.is_live(as_of))

    def expiring_within(self, days: int, as_of: dt.date) -> tuple[Override, ...]:
        horizon = as_of + dt.timedelta(days=days)
        return tuple(o for o in self._overrides if as_of <= o.expires_on <= horizon)


def load_register(root: Path | str = ".") -> OverrideRegister:
    """Load `overrides/**/*.yaml`. A malformed override is a build failure, not a warning."""
    root = Path(root)
    directory = root / OVERRIDES_DIR
    overrides: list[Override] = []
    sources: dict[int, Path] = {}
    if not directory.is_dir():
        return OverrideRegister((), {})

    for path in sorted(directory.rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is None:
            continue
        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            override = Override.model_validate(entry)
            overrides.append(override)
            sources[id(override)] = path.relative_to(root)
    return OverrideRegister(tuple(overrides), sources)


def decide(
    *,
    reason_code: str,
    tenant: str,
    datapoint_id: str,
    period: str,
    register: OverrideRegister,
    as_of: dt.date,
) -> tuple[Outcome, Override | None]:
    """What happens to a datapoint that could not be stated.

    A lawful omission never reaches here — it is already an answer. For an internal failure
    the register is consulted, and the default, with no live override, is to block.
    """
    resolved = reason_codes.resolve(reason_code)
    if resolved.is_lawful:
        raise ValueError(
            f"{reason_code} is a lawful omission and does not need an outcome decision"
        )
    override = register.find(
        tenant=tenant,
        datapoint_id=datapoint_id,
        period=period,
        reason_code=reason_code,
        as_of=as_of,
    )
    if override is None:
        return Outcome.BLOCKED, None
    return override.outcome, override
