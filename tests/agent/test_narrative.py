"""Where prose comes from, and what happens when it cannot.

The defect these cover is not subtle once stated: the deployed agent wired no narrative
provider at all, so every narrative datapoint abstained with an internal failure and blocked
the report — while the CLI passed a paragraph written by hand in Python straight into a
rendered DOCX. One path refused to publish; the other published fiction.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from attestor.agent import narrative
from attestor.agent.narrative import (
    NarrativeUnavailable,
    RecordedNarrativeProvider,
    StaleNarrativeDraft,
)
from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.datapoints.backends import RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import ResolutionContext, Resolved, Resolver
from attestor.security import injection

TRANSITION_PLAN = "ESRS_E1-1_transition_plan"


def _context(tenant: str = "helios") -> ResolutionContext:
    return ResolutionContext(
        tenant=tenant,
        period="2026",
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2027, 1, 1),
        as_of=dt.date(2026, 7, 1),
    )


# ── Every committed draft survives the controls that will judge it ───────────


@pytest.mark.parametrize("tenant", ["helios", "aegis", "lumen"])
def test_every_recorded_draft_passes_the_draft_check(
    repo_root: Path, contract_set: ContractSet, tenant: str
) -> None:
    """A fixture that would fail the gates is a fixture testing a program nobody ships."""
    provider = RecordedNarrativeProvider.from_root(repo_root)
    narratives = [c for c in contract_set if c.is_model_authored]
    checked = 0
    for contract in narratives:
        try:
            draft = provider(contract, _context(tenant))
        except NarrativeUnavailable:
            continue  # this tenant does not report under that standard
        verdict = injection.check_draft(
            text=draft.text,
            citations=draft.citations,
            retrieved_ids=frozenset(draft.citations),
            min_citations=contract.resolver.grounding.min_citations,
            max_words=contract.resolver.max_words,
        )
        assert verdict.ok, f"{tenant}/{contract.id}: {verdict.problems}"
        checked += 1
    assert checked, f"{tenant} has no recorded narrative to check"


def test_no_recorded_draft_contains_a_digit(repo_root: Path) -> None:
    """The absolute rule, asserted against the committed file rather than against a helper."""
    provider = RecordedNarrativeProvider.from_root(repo_root)
    for entry in provider._drafts.values():
        prose = injection.CITATION_MARKER.sub(" ", entry["text"])
        assert not any(char.isdigit() for char in prose), entry["datapoint_id"]


def test_every_quoted_marker_is_a_declared_citation(repo_root: Path) -> None:
    provider = RecordedNarrativeProvider.from_root(repo_root)
    for entry in provider._drafts.values():
        declared = {c.split(":", 1)[-1] for c in entry["citations"]}
        quoted = {
            m.group(0).strip("[]").split(":", 1)[1]
            for m in injection.CITATION_MARKER.finditer(entry["text"])
        }
        assert quoted <= declared, entry["datapoint_id"]


# ── Staleness ────────────────────────────────────────────────────────────────


def test_a_draft_captured_against_an_older_prompt_is_refused(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """Editing a prompt must not leave the previous prompt's prose in the report.

    This is the narrative half of `StaleRecording`. Without it every gate stays green — the
    prose is perfectly valid, for a prompt nobody is using any more.
    """
    provider = RecordedNarrativeProvider.from_root(repo_root)
    key = RecordedNarrativeProvider._key("helios", TRANSITION_PLAN)
    provider._drafts[key] = {**provider._drafts[key], "prompt_digest": "0" * 64}
    with pytest.raises(StaleNarrativeDraft, match=r"Re-capture"):
        provider(contract_set[TRANSITION_PLAN], _context())


def test_a_missing_draft_is_a_refusal_not_an_invention(
    repo_root: Path, contract_set: ContractSet
) -> None:
    provider = RecordedNarrativeProvider({}, prompts_dir=repo_root / "prompts")
    with pytest.raises(NarrativeUnavailable, match="never written here"):
        provider(contract_set[TRANSITION_PLAN], _context())


# ── The provider is actually wired ───────────────────────────────────────────


def test_the_switch_returns_a_recorded_provider_offline(repo_root: Path, monkeypatch) -> None:
    monkeypatch.delenv("ATTESTOR_BACKEND", raising=False)
    assert isinstance(narrative.build(repo_root), RecordedNarrativeProvider)


def test_a_live_provider_without_a_session_is_refused(repo_root: Path) -> None:
    """There is no ambient tenant, and retrieval needs one."""
    with pytest.raises(NarrativeUnavailable, match="session"):
        narrative.build(repo_root, session=None, backend="athena")


def test_a_narrative_datapoint_resolves_rather_than_blocking(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """The regression that mattered: with no provider this abstained and blocked the report."""
    resolver = Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=RecordedNarrativeProvider.from_root(repo_root),
    )
    results = resolver.resolve_all(_context())
    outcome = results[TRANSITION_PLAN]
    assert isinstance(outcome, Resolved)
    assert outcome.narrative
    assert results.can_issue


def test_no_provider_still_refuses_rather_than_inventing(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """The fail-closed behaviour is kept; what changed is that a provider now exists."""
    resolver = Resolver(
        contracts=contract_set.for_standard(Standard.ESRS),
        backend=RecordedBackend.from_directory(repo_root / "recordings"),
        evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
        override_register=overrides.load_register(repo_root),
        root=repo_root,
        narrative_provider=None,
    )
    outcome = resolver.resolve_all(_context())[TRANSITION_PLAN]
    assert not outcome.is_published
    assert outcome.reason_code == "E_METHOD_UNAVAILABLE"


def test_the_captures_name_the_model_they_came_from(repo_root: Path) -> None:
    """A capture that does not say which model wrote it is a claim about "the model" that
    survives replacing it.

    The prompt digest already catches a prompt edit. Nothing caught a model change: swap the
    family and every committed draft replays unchanged while still being presented as what
    the deployed model produces. `seed_recordings.py --check` closes that, because the model
    is read from `infra/agent/variables.tf` rather than typed — so changing the default
    rewrites the file and the check asks for a re-capture.
    """
    payload = yaml.safe_load((repo_root / "recordings" / "narratives.yaml").read_text())
    variables = (repo_root / "infra" / "agent" / "variables.tf").read_text(encoding="utf-8")
    block = variables.split('variable "reasoning_model"', 1)[1]
    configured = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in block.splitlines()
        if line.strip().startswith("default")
    )
    assert payload["model"] == configured, (
        "the committed drafts were captured against a different model than infra/agent "
        "deploys; run `python scripts/seed_recordings.py`"
    )
