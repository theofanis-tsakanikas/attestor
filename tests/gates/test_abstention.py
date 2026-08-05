"""Claim 5."""

from __future__ import annotations

from pathlib import Path

import pytest

from attestor.gates import abstention


@pytest.fixture(scope="module")
def scored(request) -> abstention.AbstentionScore:
    return abstention.run(Path(request.config.rootpath))


@pytest.mark.eval
def test_no_gap_ever_produces_a_figure(scored: abstention.AbstentionScore) -> None:
    """Zero fabrications. No threshold, no tolerance."""
    assert scored.fabrications == 0, scored.report()


@pytest.mark.eval
def test_every_gap_produces_the_expected_refusal(scored: abstention.AbstentionScore) -> None:
    assert scored.observed_abstentions == scored.expected_abstentions, scored.report()


@pytest.mark.eval
def test_nothing_abstains_that_was_not_damaged(scored: abstention.AbstentionScore) -> None:
    """A system that refuses everything scores perfectly on the other two and is useless."""
    assert scored.passed, scored.report()


@pytest.mark.eval
def test_the_scenarios_cover_every_way_evidence_can_fail(
    request: pytest.FixtureRequest,
) -> None:
    scenarios = abstention.load_scenarios(
        Path(request.config.rootpath) / "evals" / "abstention" / "scenarios.yaml"
    )
    reasons = {e.reason for s in scenarios for e in s.expect.values()}
    assert reasons >= {
        "E_NO_EVIDENCE",
        "E_PARTIAL_BOUNDARY",
        "E_EVIDENCE_OUT_OF_PERIOD",
        "E_UPSTREAM_QUARANTINE",
        "E_OUT_OF_TOLERANCE",
    }


@pytest.mark.eval
def test_an_override_changes_the_outcome_but_not_the_reason(
    scored: abstention.AbstentionScore,
) -> None:
    """AB-03 and AB-04 are the same gap on either side of an expiry date."""
    live = next(r for r in scored.results if r.scenario.id == "AB-03")
    lapsed = next(r for r in scored.results if r.scenario.id == "AB-04")
    assert live.scenario.expect["ESRS_E1-6_gross_scope_3"].reason == (
        lapsed.scenario.expect["ESRS_E1-6_gross_scope_3"].reason
    )
    assert live.scenario.expect["ESRS_E1-6_gross_scope_3"].outcome != (
        lapsed.scenario.expect["ESRS_E1-6_gross_scope_3"].outcome
    )
    assert live.passed and lapsed.passed, scored.report()
