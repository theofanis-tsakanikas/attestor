"""The abstention vocabulary is closed, partitioned, and cannot be softened.

These tests exist because the failure they guard against is invisible in review. Adding a
lawful-omission disposition to `E_RESOLVER_ERROR` is a one-word diff that turns every
pipeline crash into "not material" — and the report still ships, still passes every other
gate, and is wrong.
"""

from __future__ import annotations

import pytest

from attestor.contracts import reason_codes
from attestor.contracts.reason_codes import (
    BLOCKING_CODES,
    LAWFUL_CODES,
    Disposition,
    UnknownReasonCode,
)


def test_vocabulary_is_partitioned() -> None:
    assert LAWFUL_CODES.isdisjoint(BLOCKING_CODES)
    assert LAWFUL_CODES | BLOCKING_CODES == reason_codes.ALL_CODES


def test_every_code_has_exactly_one_disposition() -> None:
    for code in reason_codes.BY_CODE.values():
        assert code.is_lawful is not code.blocks_report


@pytest.mark.parametrize(
    "code",
    ["E_RESOLVER_ERROR", "E_UPSTREAM_QUARANTINE", "E_NO_EVIDENCE", "E_PARTIAL_BOUNDARY"],
)
def test_pipeline_failures_are_never_lawful(code: str) -> None:
    """The whole point. A bug is not a regulatory exemption."""
    resolved = reason_codes.resolve(code)
    assert resolved.disposition is Disposition.INTERNAL_FAILURE
    assert resolved.blocks_report
    assert not resolved.is_lawful


def test_lawful_omissions_cite_their_basis() -> None:
    """An omission a reader must accept has to say which clause permits it."""
    for code in LAWFUL_CODES:
        assert reason_codes.resolve(code).basis, f"{code} claims to be lawful but cites nothing"


def test_internal_failures_cite_nothing() -> None:
    """There is no clause that permits shipping a broken number, so there is nothing to cite."""
    for code in BLOCKING_CODES:
        assert reason_codes.resolve(code).basis == ""


def test_unknown_code_is_fatal() -> None:
    with pytest.raises(UnknownReasonCode) as excinfo:
        reason_codes.resolve("E_TOTALLY_FINE_ACTUALLY")
    assert "closed" in str(excinfo.value)


def test_disclosure_substitutes_only_the_two_allowed_fields() -> None:
    text = reason_codes.render_disclosure(
        "E_NOT_MATERIAL", datapoint="ESRS_E1-6_gross_scope_3", reference="ESRS E1-6 §44(c)"
    )
    assert "ESRS_E1-6_gross_scope_3" in text
    assert "ESRS E1-6 §44(c)" in text
    assert "{" not in text and "}" not in text


def test_disclosure_templates_take_no_other_substitution() -> None:
    """No free-text field exists, so model-authored prose has no route onto the page."""
    for code in reason_codes.BY_CODE.values():
        rendered = code.disclosure_template.format(datapoint="D", reference="R")
        assert "{" not in rendered, f"{code.code} template has an unexpected substitution"


def test_blocking_disclosures_announce_themselves() -> None:
    """A blocked datapoint must never read like an acceptable answer."""
    for code in BLOCKING_CODES:
        assert reason_codes.resolve(code).disclosure_template.startswith("BLOCKED:")
    for code in LAWFUL_CODES:
        assert reason_codes.resolve(code).disclosure_template.startswith("Not disclosed:")
