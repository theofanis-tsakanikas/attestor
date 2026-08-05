"""Claim 2, and the policy engine underneath it."""

from __future__ import annotations

from pathlib import Path

import pytest

from attestor.agent.cache import CacheKey, CacheScopeError, TenantCache
from attestor.policy import cedar
from attestor.policy.cedar import PolicyError, PolicySet
from attestor.policy.tenants import Session, TenantRegistry, UnknownRole
from attestor.security import isolation


@pytest.fixture(scope="module")
def policies(request) -> PolicySet:
    return cedar.load(Path(request.config.rootpath))


@pytest.fixture(scope="module")
def registry(request) -> TenantRegistry:
    return TenantRegistry.load(Path(request.config.rootpath))


def _session(tenant: str = "helios", role: str = "role:preparer") -> Session:
    return Session(
        tenant=tenant,
        subject="user-1",
        roles=frozenset({role}),
        period="2026",
        session_id="test-session",
    )


# ── The twelve paths ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def isolation_report(request) -> isolation.IsolationReport:
    return isolation.run(Path(request.config.rootpath))


@pytest.mark.eval
def test_no_path_leaks(isolation_report: isolation.IsolationReport) -> None:
    assert isolation_report.passed, isolation_report.report()


@pytest.mark.eval
def test_all_twelve_paths_run(isolation_report: isolation.IsolationReport) -> None:
    assert len(isolation_report.results) == 12
    assert [r.number for r in isolation_report.results] == list(range(1, 13))


@pytest.mark.eval
def test_each_probe_explains_itself(isolation_report: isolation.IsolationReport) -> None:
    """`closed` with no detail is indistinguishable from a probe that did nothing."""
    for result in isolation_report.results:
        assert result.detail.strip(), result.name


# ── Cedar semantics ──────────────────────────────────────────────────────────


def test_forbid_beats_permit(policies: PolicySet) -> None:
    session = _session()
    allowed = policies.is_authorized(
        session.request("resolve_datapoint", session.resource("Datapoint", "X"))
    )
    denied = policies.is_authorized(
        session.request("resolve_datapoint", session.resource("Datapoint", "X", tenant="aegis"))
    )
    assert allowed.allowed
    assert not denied.allowed
    assert "forbid-cross-tenant" in denied.determining


def test_default_is_deny(policies: PolicySet) -> None:
    """An action no policy mentions is refused, not permitted."""
    session = _session()
    decision = policies.is_authorized(
        session.request("delete_everything", session.resource("Datapoint", "X"))
    )
    assert not decision.allowed
    assert decision.reason == "no policy permits this request"


def test_a_reporter_cannot_render(policies: PolicySet) -> None:
    session = _session(role="role:reporter")
    assert not policies.is_authorized(
        session.request("render_report", session.resource("Report", "2026"))
    )


def test_a_preparer_can_render(policies: PolicySet) -> None:
    session = _session(role="role:preparer")
    assert policies.is_authorized(
        session.request("render_report", session.resource("Report", "2026"))
    )


def test_nobody_approves_an_override_through_the_agent(policies: PolicySet) -> None:
    """A second, weaker path to a signature is the path that gets used."""
    for role in ("role:reporter", "role:preparer", "role:auditor"):
        session = _session(role=role)
        decision = policies.is_authorized(
            session.request("approve_override", session.resource("Override", "x"))
        )
        assert not decision.allowed
        assert "forbid-approval-through-the-agent" in decision.determining


def test_an_auditor_reads_and_writes_nothing(policies: PolicySet) -> None:
    session = _session(role="role:auditor")
    assert policies.is_authorized(session.request("read_lineage", session.resource("Lineage", "x")))
    assert not policies.is_authorized(
        session.request("render_report", session.resource("Report", "2026"))
    )


# ── Parser ───────────────────────────────────────────────────────────────────


def test_every_committed_policy_carries_an_id(policies: PolicySet) -> None:
    """A generated id means a policy nobody named, and a denial nobody can trace."""
    assert all("#" not in policy_id for policy_id in policies.ids)


def test_an_unsupported_construct_is_a_parse_error() -> None:
    """Failing to parse must not mean failing open."""
    with pytest.raises(PolicyError):
        cedar.parse('permit (principal, action, resource) when { principal.tenant like "h*" };')


def test_a_missing_attribute_is_an_error_not_a_pass() -> None:
    """A typo in a condition must not become an allow."""
    parsed = cedar.parse(
        '@id("t") permit (principal, action, resource) when { resource.owner == "x" };'
    )
    session = _session()
    request = session.request("resolve_datapoint", session.resource("Datapoint", "X"))
    with pytest.raises(PolicyError, match="not present on the request"):
        parsed[0].applies(request)


def test_a_non_empty_file_that_parses_to_nothing_is_refused() -> None:
    with pytest.raises(PolicyError, match="no policy could be parsed"):
        cedar.parse("this is not cedar", source="junk.cedar")


# ── Tenants and sessions ─────────────────────────────────────────────────────


def test_three_tenants_two_verticals(registry: TenantRegistry) -> None:
    assert registry.ids == ("aegis", "helios", "lumen")
    assert {t.standard for t in registry} == {"ESRS", "EU_AI_ACT"}


def test_namespaces_are_derived_not_configured(registry: TenantRegistry) -> None:
    """A copy-paste cannot point two tenants at one namespace."""
    namespaces = [tenant.namespace for tenant in registry]
    assert len(set(namespaces)) == len(namespaces)


def test_tenants_do_not_all_share_one_identity_provider(registry: TenantRegistry) -> None:
    """Otherwise 'identity is per tenant' is a fact about config, not about the design."""
    assert len({tenant.identity.kind for tenant in registry}) > 1
    assert len({tenant.identity.groups_claim for tenant in registry}) > 1


def test_a_role_arrives_from_claims_not_from_a_conversation(registry: TenantRegistry) -> None:
    session = Session.from_claims(
        {"sub": "user-1", "cognito:groups": ["helios-preparers"]},
        tenant=registry["helios"],
        period="2026",
        session_id="sess-01",
    )
    assert session.roles == frozenset({"role:preparer"})


def test_an_unmapped_group_does_not_become_a_role(registry: TenantRegistry) -> None:
    with pytest.raises(UnknownRole):
        Session.from_claims(
            {"sub": "user-1", "cognito:groups": ["some-other-group"]},
            tenant=registry["helios"],
            period="2026",
            session_id="sess-01",
        )


def test_a_session_with_no_role_cannot_exist() -> None:
    with pytest.raises(ValueError, match="should not exist"):
        Session(
            tenant="helios",
            subject="user-1",
            roles=frozenset(),
            period="2026",
            session_id="sess-01",
        )


def test_the_retrieval_filter_comes_from_the_session() -> None:
    assert _session().retrieval_filter() == {"tenant": "helios", "period": "2026"}


# ── Cache ────────────────────────────────────────────────────────────────────


def test_the_same_question_from_two_tenants_hashes_differently() -> None:
    a = CacheKey.of(tenant="helios", period="2026", kind="retrieval", q="scope 1")
    b = CacheKey.of(tenant="aegis", period="2026", kind="retrieval", q="scope 1")
    assert a.digest != b.digest


def test_a_foreign_key_is_refused_rather_than_missed() -> None:
    """A miss would be safe here; a refusal is safe *and* audible."""
    cache = TenantCache("helios")
    with pytest.raises(CacheScopeError):
        cache.get(CacheKey.of(tenant="aegis", period="2026", kind="retrieval", q="x"))


def test_a_cache_serves_its_own_tenant() -> None:
    cache = TenantCache("helios")
    key = CacheKey.of(tenant="helios", period="2026", kind="retrieval", q="x")
    cache.put(key, ["passage"])
    assert cache.get(key) == ["passage"]
    assert cache.hits == 1
