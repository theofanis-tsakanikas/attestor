"""Which backend a run uses, and why the default is the safe one in both directions.

The defect this guards against was live and quiet: `attestor run` hardcoded the recorded
backend, so the deploy workflow's "run against the live estate" step replayed the fixtures and
printed PASS without touching Athena. A deploy that appears to succeed while proving nothing
is worse than one that fails, because nobody goes looking.
"""

from __future__ import annotations

import pytest
import typer

from attestor.agent import narrative
from attestor.cli import main as cli
from attestor.contracts import loader
from attestor.datapoints.backends import AthenaBackend, RecordedBackend
from attestor.policy.tenants import Session, TenantRegistry


def test_the_default_is_recorded(repo_root, monkeypatch) -> None:
    """Every gate replays recordings; reaching for an account by default would need creds."""
    monkeypatch.delenv("ATTESTOR_BACKEND", raising=False)
    assert isinstance(cli._backend(repo_root), RecordedBackend)


def test_athena_is_selected_explicitly(repo_root, monkeypatch) -> None:
    monkeypatch.setenv("ATTESTOR_BACKEND", "athena")
    monkeypatch.setenv("ATTESTOR_WORKGROUP", "attestor")
    monkeypatch.setenv("ATTESTOR_DATABASE", "attestor_gold")
    monkeypatch.setenv("ATTESTOR_ATHENA_OUTPUT", "s3://bucket/results/")
    assert isinstance(cli._backend(repo_root), AthenaBackend)


def test_athena_without_its_environment_fails_rather_than_falling_back(
    repo_root, monkeypatch
) -> None:
    """Falling back here would produce a green run that queried nothing — the original bug."""
    monkeypatch.setenv("ATTESTOR_BACKEND", "athena")
    for name in ("ATTESTOR_WORKGROUP", "ATTESTOR_DATABASE", "ATTESTOR_ATHENA_OUTPUT"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(typer.Exit):
        cli._backend(repo_root)


# --- the live narrative provider ---------------------------------------------------------
#
# `narrative.build`'s athena branch is the one path the whole offline suite never takes, and it
# has now failed in a deploy three separate times: once on a missing session, once on an env
# var, once on `ContractSet.values()` — a method that does not exist, in a line that had been
# green on every gate because no test ever reached it. Constructing the provider needs no
# credentials; only *using* it does. So construct it.


def _live_environment(monkeypatch) -> None:
    monkeypatch.setenv("ATTESTOR_BACKEND", "athena")
    monkeypatch.setenv("ATTESTOR_REASONING_MODEL", "model")
    monkeypatch.setenv("ATTESTOR_GUARDRAIL_ID", "guardrail")
    monkeypatch.setenv("ATTESTOR_GUARDRAIL_VER", "1")
    monkeypatch.setenv("ATTESTOR_EVIDENCE_KB", "EVKB")
    monkeypatch.setenv("ATTESTOR_REGULATORY_KB", "REGKB")


@pytest.mark.parametrize("tenant", ["helios", "aegis", "lumen"])
def test_the_live_provider_can_be_built(repo_root, monkeypatch, tenant) -> None:
    _live_environment(monkeypatch)
    provider = narrative.build(repo_root, session=_session(repo_root, tenant))
    assert provider.__class__.__name__ == "BedrockNarrativeProvider"


@pytest.mark.parametrize(
    ("tenant", "prefix", "foreign"),
    [("helios", "ESRS_", "AIACT_"), ("aegis", "ESRS_", "AIACT_"), ("lumen", "AIACT_", "ESRS_")],
)
def test_a_tenant_is_offered_only_its_own_standards_placeholders(
    repo_root, monkeypatch, tenant, prefix, foreign
) -> None:
    """Offering `lumen` an ESRS figure invites a placeholder the renderer will refuse.

    The refusal would be correct and the cause would be us: the model can only name what it
    was shown.
    """
    _live_environment(monkeypatch)
    offered = narrative.build(repo_root, session=_session(repo_root, tenant)).placeholder_ids

    assert offered, "a narrative with no placeholders can only write prose with no figures"
    assert all(name.startswith(prefix) for name in offered)
    assert not any(name.startswith(foreign) for name in offered)


def test_no_narrative_is_offered_as_a_placeholder(repo_root, monkeypatch) -> None:
    """A narrative pointing at a narrative is a citation loop, not a figure."""

    _live_environment(monkeypatch)
    offered = set(narrative.build(repo_root, session=_session(repo_root, "helios")).placeholder_ids)
    authored = {c.id for c in loader.load(repo_root) if c.is_model_authored}

    assert offered.isdisjoint(authored)


def _session(root, tenant: str):

    config = TenantRegistry.load(root)[tenant]
    return Session.from_claims(
        claims={
            "sub": "live-build-test",
            "iss": config.identity.issuer,
            "aud": config.identity.audience,
            config.identity.groups_claim: [next(iter(config.identity.role_map))],
        },
        tenant=config,
        session_id="livebuild",
        period="2026",
    )
