"""The break-glass path, and everything it refuses.

ADR-0001 says a control with no override does not prevent the override — it moves it outside
the system, where it leaves no evidence. So there is a key. These tests are about the shape
of the keyhole.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from pydantic import ValidationError

from attestor.contracts import overrides
from attestor.contracts.overrides import (
    NotOverridable,
    Outcome,
    Override,
    OverrideEffect,
    OverrideRegister,
)
from attestor.contracts.reason_codes import BLOCKING_CODES

SIGNED = dt.date(2026, 6, 19)
LIVE = dt.date(2026, 7, 1)
LAPSED = dt.date(2026, 12, 1)


def _override(**changes) -> Override:
    payload = {
        "tenant": "helios",
        "datapoint_id": "ESRS_E1-6_gross_scope_3",
        "period": "2026",
        "reason_code": "E_PARTIAL_BOUNDARY",
        "effect": "omit_with_material_limitation",
        "justification": (
            "Three subcontracted hauliers did not respond within the reporting window and a "
            "spend-based estimate was rejected as indistinguishable from measured data."
        ),
        "requested_by": "Sustainability Data Lead",
        "approvals": [
            {
                "approver": "M. Andreadis",
                "role": "head_of_sustainability_reporting",
                "approved_on": "2026-06-18",
                "evidence_reference": "SUS-BOARD-2026-041",
            },
            {
                "approver": "K. Vlachou",
                "role": "chief_financial_officer",
                "approved_on": "2026-06-19",
                "evidence_reference": "SUS-BOARD-2026-041",
            },
        ],
        "expires_on": "2026-09-15",
    }
    payload.update(changes)
    return Override.model_validate(payload)


# ── The door with no key ─────────────────────────────────────────────────────


def test_resolver_error_cannot_be_overridden_by_anyone() -> None:
    """The one absolute refusal. A crashed resolver is an unknown deficiency."""
    with pytest.raises(ValidationError) as excinfo:
        _override(reason_code="E_RESOLVER_ERROR")
    assert "cannot be overridden by anyone" in str(excinfo.value)


def test_resolver_error_rule_has_no_permitted_effect() -> None:
    assert not overrides.RULES["E_RESOLVER_ERROR"].is_overridable


def test_not_overridable_is_raised_as_such() -> None:
    """Callers ask this without building an Override — the resolver asks before offering a path."""
    with pytest.raises(NotOverridable):
        overrides.ensure_overridable(
            "E_RESOLVER_ERROR", OverrideEffect.OMIT_WITH_MATERIAL_LIMITATION
        )


def test_every_blocking_code_has_an_explicit_rule() -> None:
    """No internal failure gets an implicit default — each is a deliberate decision."""
    assert set(overrides.RULES) == set(BLOCKING_CODES)


# ── An override never launders a defect ──────────────────────────────────────


def test_a_lawful_omission_needs_no_override() -> None:
    with pytest.raises(ValidationError, match="needs no override"):
        _override(reason_code="E_NOT_MATERIAL")


def test_only_out_of_tolerance_may_still_publish_a_figure() -> None:
    """Everywhere else there is no figure to publish, so publishing is not on offer."""
    publishable = {
        code
        for code, rule in overrides.RULES.items()
        if OverrideEffect.PUBLISH_WITH_QUALIFICATION in rule.permitted_effects
    }
    assert publishable == {"E_OUT_OF_TOLERANCE"}


def test_missing_evidence_cannot_be_overridden_into_a_published_figure() -> None:
    with pytest.raises(ValidationError, match="may not be overridden to"):
        _override(reason_code="E_NO_EVIDENCE", effect="publish_with_qualification")


def test_the_reason_code_survives_the_override() -> None:
    """`E_PARTIAL_BOUNDARY` is still `E_PARTIAL_BOUNDARY` afterwards. Nothing is relabelled."""
    override = _override()
    assert override.reason_code == "E_PARTIAL_BOUNDARY"
    assert "E_PARTIAL_BOUNDARY" in override.render_limitation(reference="ESRS E1-6 §44(c)")


# ── Only a named human turns the key ─────────────────────────────────────────


@pytest.mark.parametrize(
    "approver",
    [
        "arn:aws:iam::123456789012:role/attestor-deploy",
        "attestor-report-role",
        "svc-reporting",
        "reporting-sa",
        "github-actions[bot]",
        "Attestor Agent",
        "reporting service account",
        "ci-runner",
    ],
)
def test_automated_principals_cannot_approve(approver: str) -> None:
    """The system may never open a door for itself, and an agent cannot be argued into it."""
    with pytest.raises(ValidationError, match="automated principal"):
        _override(
            approvals=[
                {
                    "approver": approver,
                    "role": "chief_financial_officer",
                    "approved_on": "2026-06-18",
                    "evidence_reference": "SUS-BOARD-2026-041",
                },
                {
                    "approver": "K. Vlachou",
                    "role": "head_of_sustainability_reporting",
                    "approved_on": "2026-06-19",
                    "evidence_reference": "SUS-BOARD-2026-041",
                },
            ]
        )


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown approver role"):
        _override(
            approvals=[
                {
                    "approver": "A. Person",
                    "role": "intern",
                    "approved_on": "2026-06-18",
                    "evidence_reference": "SUS-BOARD-2026-041",
                }
            ]
        )


def test_requester_cannot_approve_their_own_override() -> None:
    with pytest.raises(ValidationError, match="cannot also approve"):
        _override(requested_by="M. Andreadis")


def test_the_same_person_cannot_sign_twice() -> None:
    approval = {
        "approver": "M. Andreadis",
        "role": "head_of_sustainability_reporting",
        "approved_on": "2026-06-18",
        "evidence_reference": "SUS-BOARD-2026-041",
    }
    with pytest.raises(ValidationError, match="cannot sign an override twice"):
        _override(approvals=[approval, {**approval, "approved_on": "2026-06-19"}])


def test_dual_approval_is_enforced_where_the_rule_demands_it() -> None:
    with pytest.raises(ValidationError, match="requires 2 approval"):
        _override(
            approvals=[
                {
                    "approver": "M. Andreadis",
                    "role": "head_of_sustainability_reporting",
                    "approved_on": "2026-06-18",
                    "evidence_reference": "SUS-BOARD-2026-041",
                }
            ]
        )


def test_method_unavailable_demands_senior_approval() -> None:
    """Changing methodology mid-period is not a reporting-team decision."""
    with pytest.raises(ValidationError, match="may not approve"):
        _override(
            reason_code="E_METHOD_UNAVAILABLE",
            approvals=[
                {
                    "approver": "M. Andreadis",
                    "role": "head_of_sustainability_reporting",
                    "approved_on": "2026-06-18",
                    "evidence_reference": "SUS-BOARD-2026-041",
                },
                {
                    "approver": "K. Vlachou",
                    "role": "chief_financial_officer",
                    "approved_on": "2026-06-19",
                    "evidence_reference": "SUS-BOARD-2026-041",
                },
            ],
        )


def test_justification_must_say_something() -> None:
    with pytest.raises(ValidationError):
        _override(justification="deadline")


# ── Overrides expire ─────────────────────────────────────────────────────────


def test_override_cannot_outlive_its_rule() -> None:
    with pytest.raises(ValidationError, match="last at most 90 days"):
        _override(expires_on="2027-06-01")


def test_override_expiring_before_it_was_signed_is_rejected() -> None:
    with pytest.raises(ValidationError, match="is not one"):
        _override(expires_on="2026-06-01")


def test_liveness_is_a_function_of_the_date() -> None:
    override = _override()
    assert override.is_live(LIVE)
    assert not override.is_live(LAPSED)


# ── The register ─────────────────────────────────────────────────────────────


def _register(*items: Override) -> OverrideRegister:
    return OverrideRegister(items, {})


def test_default_outcome_with_no_override_is_blocked() -> None:
    outcome, override = overrides.decide(
        reason_code="E_NO_EVIDENCE",
        tenant="helios",
        datapoint_id="ESRS_E1-6_gross_scope_1",
        period="2026",
        register=_register(),
        as_of=LIVE,
    )
    assert outcome is Outcome.BLOCKED
    assert override is None


def test_a_live_override_changes_the_outcome() -> None:
    outcome, override = overrides.decide(
        reason_code="E_PARTIAL_BOUNDARY",
        tenant="helios",
        datapoint_id="ESRS_E1-6_gross_scope_3",
        period="2026",
        register=_register(_override()),
        as_of=LIVE,
    )
    assert outcome is Outcome.OMITTED_WITH_MATERIAL_LIMITATION
    assert override is not None


def test_a_lapsed_override_blocks_again() -> None:
    """This is the whole point of an expiry date."""
    outcome, _ = overrides.decide(
        reason_code="E_PARTIAL_BOUNDARY",
        tenant="helios",
        datapoint_id="ESRS_E1-6_gross_scope_3",
        period="2026",
        register=_register(_override()),
        as_of=LAPSED,
    )
    assert outcome is Outcome.BLOCKED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant", "aegis"),
        ("datapoint_id", "ESRS_E1-6_gross_scope_1"),
        ("period", "2027"),
        ("reason_code", "E_NO_EVIDENCE"),
    ],
)
def test_an_override_covers_exactly_what_it_says(field: str, value: str) -> None:
    """Signed for Scope 3 in 2026 at helios means exactly that. Nothing here is fuzzy."""
    register = _register(_override())
    query = {
        "tenant": "helios",
        "datapoint_id": "ESRS_E1-6_gross_scope_3",
        "period": "2026",
        "reason_code": "E_PARTIAL_BOUNDARY",
        field: value,
    }
    outcome, _ = overrides.decide(register=register, as_of=LIVE, **query)
    assert outcome is Outcome.BLOCKED


def test_lawful_omissions_do_not_reach_the_decision() -> None:
    with pytest.raises(ValueError, match="does not need an outcome decision"):
        overrides.decide(
            reason_code="E_PHASE_IN",
            tenant="helios",
            datapoint_id="ESRS_E1-6_gross_scope_3",
            period="2026",
            register=_register(),
            as_of=LIVE,
        )


# ── What the reader sees ─────────────────────────────────────────────────────


def test_the_limitation_names_the_signatories_and_the_expiry() -> None:
    text = _override().render_limitation(reference="ESRS E1-6 §44(c)")
    assert "Material limitation" in text
    assert "M. Andreadis (head_of_sustainability_reporting)" in text
    assert "K. Vlachou (chief_financial_officer)" in text
    assert "2026-09-15" in text
    assert "ESRS E1-6 §44(c)" in text


def test_a_qualified_publication_reads_differently() -> None:
    override = _override(
        reason_code="E_OUT_OF_TOLERANCE",
        effect="publish_with_qualification",
        expires_on="2026-08-10",
    )
    text = override.render_limitation(reference="ESRS E1-6 §44(a)")
    assert text.startswith("Qualified disclosure:")
    assert "disagree beyond" in text or "disagree" in text


# ── The committed register ───────────────────────────────────────────────────


def test_the_repository_register_loads(repo_root: Path) -> None:
    register = overrides.load_register(repo_root)
    assert len(register) >= 1


def test_no_committed_override_touches_the_door_with_no_key(repo_root: Path) -> None:
    for override in overrides.load_register(repo_root):
        assert override.reason_code != "E_RESOLVER_ERROR"
