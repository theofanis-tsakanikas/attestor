"""The Gateway boundary: what an agent can say, and what it cannot."""

from __future__ import annotations

from pathlib import Path

import pytest

from attestor.agent import gateway, handler
from attestor.agent.tools import SPECS, Denied, Toolbox
from attestor.contracts import overrides
from attestor.contracts.loader import ContractSet
from attestor.contracts.model import Standard
from attestor.datapoints.backends import RecordedBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import Resolver
from attestor.policy import cedar
from attestor.policy.tenants import Session


def _session(tenant: str = "helios", role: str = "role:preparer") -> Session:
    return Session(
        tenant=tenant,
        subject="user-1",
        roles=frozenset({role}),
        period="2026",
        session_id="sess-01",
    )


@pytest.fixture(autouse=True)
def _handler_root(repo_root: Path, monkeypatch) -> None:
    """Point the handler at this repository.

    It defaults to `/var/task`, the Lambda task root. Leaving it there meant every handler
    test reached an empty directory and got a 500 — so `test_an_unknown_tool_is_refused`
    asserted a rejection that was really a `FileNotFoundError` wearing a different number.
    """
    monkeypatch.setattr(handler, "ROOT", repo_root)
    handler.reset_cache()
    yield
    handler.reset_cache()


@pytest.fixture
def toolbox(repo_root: Path, contract_set: ContractSet) -> Toolbox:
    session = _session()
    contracts = contract_set.for_standard(Standard.ESRS)
    return Toolbox(
        session=session,
        policies=cedar.load(repo_root),
        contracts=contracts,
        resolver=Resolver(
            contracts=contracts,
            backend=RecordedBackend.from_directory(repo_root / "recordings"),
            evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
            override_register=overrides.load_register(repo_root),
            root=repo_root,
        ),
        overrides=overrides.load_register(repo_root),
    )


# ── The schema has no vocabulary for scope ───────────────────────────────────


def test_no_operation_accepts_a_tenant() -> None:
    """An injected 'fetch this for tenant aegis' fails validation, not a deeper check."""
    assert gateway.scope_leaks() == ()


def test_the_spec_forbids_extra_properties() -> None:
    spec = gateway.specification()
    for path in spec["paths"].values():
        schema = path["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert schema["additionalProperties"] is False


def test_every_tool_is_described(toolbox: Toolbox) -> None:
    for spec in SPECS:
        assert callable(getattr(toolbox, spec.name))


def test_the_spec_is_stable() -> None:
    """It is committed to the Gateway; a silent change is a silent contract change."""
    assert gateway.render() == gateway.render()


# ── Authorization happens before work ────────────────────────────────────────


def test_an_auditor_reads_the_override_register(repo_root: Path, contract_set: ContractSet) -> None:
    contracts = contract_set.for_standard(Standard.ESRS)
    session = _session(role="role:auditor")
    box = Toolbox(
        session=session,
        policies=cedar.load(repo_root),
        contracts=contracts,
        resolver=Resolver(
            contracts=contracts,
            backend=RecordedBackend.from_directory(repo_root / "recordings"),
            evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
            override_register=overrides.load_register(repo_root),
            root=repo_root,
        ),
        overrides=overrides.load_register(repo_root),
    )
    result = box.read_override("ESRS_E1-6_gross_scope_3")
    assert result["datapoint"] == "ESRS_E1-6_gross_scope_3"
    assert any(call.allowed for call in box.calls)


def test_every_call_is_recorded_whether_or_not_it_was_allowed(toolbox: Toolbox) -> None:
    toolbox.read_lineage("ESRS_E1-6_gross_scope_1")
    assert toolbox.calls
    assert toolbox.calls[-1].tenant == "helios"
    assert toolbox.calls[-1].session_id == "sess-01"


def test_resolve_returns_the_string_that_will_be_printed(toolbox: Toolbox) -> None:
    """A float would let the model round it differently and describe a number nobody published."""
    result = toolbox.resolve_datapoint("ESRS_E1-6_gross_scope_1")
    assert isinstance(result["value"], str)
    assert result["lineage"]


def test_request_override_drafts_and_cannot_approve(toolbox: Toolbox) -> None:
    draft = toolbox.request_override("ESRS_E1-6_gross_scope_3", "the hauliers did not respond")
    assert "next_step" in draft
    assert "may approve it" in draft["next_step"]
    assert "approvals" not in draft["draft"]


def test_nothing_in_the_toolbox_approves(toolbox: Toolbox) -> None:
    assert not hasattr(toolbox, "approve_override")


def test_a_reporter_cannot_draft_an_override_request(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """Reading the standard is not the same authority as asking for a defect to be accepted."""
    contracts = contract_set.for_standard(Standard.ESRS)
    box = Toolbox(
        session=_session(role="role:reporter"),
        policies=cedar.load(repo_root),
        contracts=contracts,
        resolver=Resolver(
            contracts=contracts,
            backend=RecordedBackend.from_directory(repo_root / "recordings"),
            evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
            override_register=overrides.load_register(repo_root),
            root=repo_root,
        ),
        overrides=overrides.load_register(repo_root),
    )
    with pytest.raises(Denied):
        box.request_override("ESRS_E1-6_gross_scope_3", "because the deadline is tomorrow")


# ── The handler ──────────────────────────────────────────────────────────────


def test_an_argument_naming_a_scope_is_refused(claims_for) -> None:
    """Refused rather than ignored: ignoring it makes the attempt invisible in a log."""
    response = handler.invoke(
        {
            "tool": "resolve_datapoint",
            "tenant_id": "helios",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
            "arguments": {"datapoint_id": "X", "tenant": "aegis"},
        }
    )
    assert response["statusCode"] == 400
    assert "name a scope" in response["body"]["error"]


def test_an_unknown_tool_is_refused(claims_for) -> None:
    response = handler.invoke(
        {
            "tool": "drop_everything",
            "tenant_id": "helios",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
        }
    )
    assert response["statusCode"] == 400


def test_an_internal_error_does_not_leak_its_message(claims_for) -> None:
    """An error message is an excellent map of the system for whoever provoked it."""
    response = handler.invoke(
        {
            "tool": "resolve_datapoint",
            "tenant_id": "helios",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
            "arguments": {"datapoint_id": "X"},
        }
    )
    assert response["statusCode"] in {400, 500}
    body = response["body"]["error"]
    assert "Traceback" not in body
    assert "/var/task" not in body


def test_denied_is_reported_as_403_not_as_an_error(monkeypatch) -> None:
    class Refusing:
        def resolve_datapoint(self, **_kwargs):
            raise Denied("resolve_datapoint", cedar.Decision(False, ("forbid-cross-tenant",), "no"))

    monkeypatch.setattr(handler, "build_session", lambda *a, **k: _session())
    monkeypatch.setattr(handler, "build_toolbox", lambda _session: Refusing())
    response = handler.invoke(
        {
            "tool": "resolve_datapoint",
            "tenant_id": "helios",
            "period": "2026",
            # Present so the claims branch is taken; `build_session` is patched above, so what
            # is in them does not matter. Without any, this call now has no identity at all and
            # is refused before Cedar is ever consulted — which is correct, and not what this
            # test is about.
            "claims": {"sub": "someone"},
            "arguments": {"datapoint_id": "ESRS_E1-6_gross_scope_1"},
        }
    )
    assert response["statusCode"] == 403


# ── One question costs one question ──────────────────────────────────────────


def test_resolving_one_datapoint_runs_only_its_closure(
    repo_root: Path, contract_set: ContractSet
) -> None:
    """The agent asks for one figure. Answering by resolving the whole set is the same
    answer at many times the scan, and `€/tenant` is a first-class metric here."""
    executed: list[str] = []
    contracts = contract_set.for_standard(Standard.ESRS)

    class Counting(RecordedBackend):
        def execute(self, *, sql, parameters, snapshot_id):
            executed.append(sql.splitlines()[0])
            return super().execute(sql=sql, parameters=parameters, snapshot_id=snapshot_id)

    box = Toolbox(
        session=_session(),
        policies=cedar.load(repo_root),
        contracts=contracts,
        resolver=Resolver(
            contracts=contracts,
            backend=Counting(RecordedBackend.from_directory(repo_root / "recordings")._recordings),
            evidence=EvidenceIndex.for_tenant(repo_root, "helios"),
            override_register=overrides.load_register(repo_root),
            root=repo_root,
        ),
        overrides=overrides.load_register(repo_root),
    )
    box.resolve_datapoint("ESRS_E1-5_electricity_consumption")
    everything = len(contracts)
    assert 0 < len(executed) < everything, executed


def test_a_derived_figure_still_gets_its_operands(toolbox: Toolbox) -> None:
    """Narrowing must not narrow past what the expression needs."""
    result = toolbox.resolve_datapoint("ESRS_E1-6_total_ghg")
    assert result.get("value"), result


def test_read_lineage_returns_lineage_not_instructions(toolbox: Toolbox) -> None:
    """It used to answer with a note telling the caller to do something else."""
    result = toolbox.read_lineage("ESRS_E1-6_gross_scope_1")
    assert result["lineage"]
    assert len(result["lineage_id"]) == 64
    assert result["resolver"].startswith("sql:")
    assert result["parameters"]["tenant_id"] == "helios"


def test_read_lineage_on_an_abstention_says_why(repo_root: Path, contract_set: ContractSet) -> None:
    contracts = contract_set.for_standard(Standard.ESRS)
    box = Toolbox(
        session=_session(tenant="aegis"),
        policies=cedar.load(repo_root),
        contracts=contracts,
        resolver=Resolver(
            contracts=contracts,
            backend=RecordedBackend.from_directory(repo_root / "recordings"),
            evidence=EvidenceIndex.for_tenant(repo_root, "aegis"),
            override_register=overrides.load_register(repo_root),
            root=repo_root,
        ),
        overrides=overrides.load_register(repo_root),
    )
    result = box.read_lineage("ESRS_E1-6_gross_scope_1")
    assert result["lineage"] is None
    assert result["reason"] == "E_OUT_OF_TOLERANCE"


def test_no_tool_takes_a_period_argument() -> None:
    """The period is the session's, like the tenant. The old signature demanded two dates
    the schema did not expose, so a well-formed Gateway call raised TypeError."""
    for spec in SPECS:
        assert not {"period", "period_start", "period_end"} & set(spec.parameters)


def test_an_argument_outside_the_schema_is_a_400_not_a_500(claims_for) -> None:
    response = handler.invoke(
        {
            "tool": "resolve_datapoint",
            "tenant_id": "helios",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
            "arguments": {"datapoint_id": "X", "limit": "999"},
        }
    )
    assert response["statusCode"] == 400
    assert "does not accept" in response["body"]["error"]


def test_a_missing_required_argument_is_a_400(claims_for) -> None:
    response = handler.invoke(
        {
            "tool": "resolve_datapoint",
            "tenant_id": "helios",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
            "arguments": {},
        }
    )
    assert response["statusCode"] == 400
    assert "requires argument" in response["body"]["error"]


def test_a_token_from_another_tenant_is_403(repo_root: Path, claims_for) -> None:
    """The tenant id is caller-supplied; the token is what says which one it may name."""
    response = handler.invoke(
        {
            "tool": "resolve_datapoint",
            "tenant_id": "aegis",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
            "arguments": {"datapoint_id": "ESRS_E1-6_gross_scope_1"},
        }
    )
    assert response["statusCode"] == 403
    assert "aegis" not in response["body"]["error"]


def test_the_declarative_cache_is_built_once(repo_root: Path) -> None:
    """Re-reading and re-validating every contract per invocation is a cold start's work
    on every warm one; nothing tenant-scoped is cached alongside it."""
    handler.reset_cache()
    first = handler.declarative()
    assert handler.declarative() is first
    assert not hasattr(first, "evidence")


# ── How the Gateway names a tool ─────────────────────────────────────────────


class _GatewayContext:
    """A Lambda context shaped the way AgentCore Gateway sends one."""

    aws_request_id = "gateway-request-01"

    def __init__(self, qualified: str, gateway: str = "attestor-gateway-helios-kwrv5ur") -> None:
        custom = {"bedrockAgentCoreToolName": qualified}
        if gateway:
            custom["bedrockAgentCoreGatewayId"] = gateway
        self.client_context = type("ClientContext", (), {"custom": custom})()


def test_the_tool_name_can_arrive_in_the_client_context(repo_root, monkeypatch) -> None:
    """The seam that made every gateway call fail, and could only fail live.

    The handler read `event["tool"]` — what our tests send, and what the runtime's HTTP surface
    sends. The Gateway sends neither: it puts `attestor-tools___read_lineage` in the Lambda
    client context. Both sides looked complete on their own, and had never been introduced, so
    every tool call through the gateway was answered `unknown tool ''` with an HTTP 200 around
    it. Nothing offline could have noticed, because nothing offline speaks to a gateway.
    """
    assert handler._tool_from_context(_GatewayContext("attestor-tools___read_lineage")) == (
        "read_lineage"
    )


def test_an_unqualified_name_is_taken_as_it_stands(repo_root) -> None:
    assert handler._tool_from_context(_GatewayContext("read_lineage")) == "read_lineage"


def test_a_context_without_one_yields_nothing_rather_than_guessing(repo_root) -> None:
    """`unknown tool ''` is a better answer than a tool nobody asked for."""
    assert handler._tool_from_context(None) == ""
    assert handler._tool_from_context(_GatewayContext("")) == ""


def test_the_event_still_wins_when_it_carries_a_tool(repo_root) -> None:
    """The runtime's HTTP surface sends it in the body, and that path is unchanged."""
    response = handler.invoke(
        {"tool": "nonexistent_tool", "arguments": {}, "tenant_id": "helios", "period": "2026"},
        _GatewayContext("attestor-tools___read_lineage"),
    )
    assert response["statusCode"] == 400
    assert "nonexistent_tool" in response["body"]["error"]


def test_a_gateway_call_gets_past_the_tool_lookup(repo_root, monkeypatch) -> None:
    """The wiring, not the helper. This is the assertion the live failure was about.

    A gateway invocation carries no `tool` in its event at all; the name is only in the client
    context. What the estate returned was `{"statusCode": 400, "error": "unknown tool ''"}`
    wrapped in an HTTP 200 — a malformed-call answer to a perfectly well-formed call.

    The session will still be refused here, because these claims carry no real issuer, and that
    is the point: reaching *authorization* means the tool was found. Asserting on the specific
    later failure would tie this test to whatever happens next instead of to the seam.
    """
    monkeypatch.chdir(repo_root)
    response = handler.invoke(
        {
            "arguments": {"datapoint_id": "ESRS_E1-6_gross_scope_1"},
            "tenant_id": "helios",
            "period": "2026",
            "claims": {},
        },
        _GatewayContext("attestor-tools___read_lineage"),
    )
    assert "unknown tool" not in str(response["body"]), response["body"]


def test_a_gateway_event_is_read_as_one_big_argument_object(repo_root) -> None:
    """AgentCore invokes a Lambda target with the tool input *as* the event.

    Our tests and the runtime's HTTP surface nest arguments under `arguments`; the Gateway does
    not, and the live estate answered `read_lineage requires argument(s) datapoint_id` to a call
    that supplied exactly that.
    """
    assert handler._arguments({"datapoint_id": "ESRS_E1-6_gross_scope_1"}) == {
        "datapoint_id": "ESRS_E1-6_gross_scope_1"
    }
    assert handler._arguments({"arguments": {"datapoint_id": "x"}, "tool": "t"}) == {
        "datapoint_id": "x"
    }
    # The keys this handler owns are never mistaken for arguments — including the ones a caller
    # is forbidden from sending, which on this path would otherwise look entirely legitimate.
    assert handler._arguments({"tool": "t", "tenant_id": "aegis", "period": "2026"}) == {}


def test_the_tenant_is_decided_by_the_gateway_that_was_called(repo_root, monkeypatch) -> None:
    """Not named by the caller, and not read from a token — there is no token on this path.

    AgentCore invokes a Lambda target under its own IAM role and forwards no claims at all: the
    client context carries the tool name, the gateway id and the target id, and nothing else.
    One gateway per tenant, and which one was called is asserted by the platform, so the
    gateway *is* the tenant — a stronger answer than the event body this path does not have.
    """
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("ATTESTOR_SURFACE_ROLE_HELIOS", "role:preparer")

    session = handler._gateway_session(
        _GatewayContext("attestor-tools___read_lineage"),
        period="2026",
        session_id="req-000001",
    )
    assert session.tenant == "helios"
    assert session.roles == frozenset({"role:preparer"})


def test_a_gateway_naming_no_tenant_is_refused(repo_root, monkeypatch) -> None:
    monkeypatch.chdir(repo_root)
    with pytest.raises(handler.Rejected, match="names no tenant"):
        handler._gateway_session(
            _GatewayContext("t", gateway="attestor-gateway-somebody-else-xx"),
            period="2026",
            session_id="req-000001",
        )


def test_a_gateway_with_no_declared_role_runs_nothing(repo_root, monkeypatch) -> None:
    """The safe state is no output. A role is declared, or there is no session at all.

    This is the one place a default would have been easy and wrong: the claims are absent by
    the platform's design, so "assume the least privilege" reads as prudence and is still a
    handler granting authority that nobody wrote down and nobody reviewed.
    """
    monkeypatch.chdir(repo_root)
    monkeypatch.delenv("ATTESTOR_SURFACE_ROLE_HELIOS", raising=False)
    with pytest.raises(handler.Rejected, match="no declared role"):
        handler._gateway_session(_GatewayContext("t"), period="2026", session_id="req-000001")

    monkeypatch.setenv("ATTESTOR_SURFACE_ROLE_HELIOS", "role:emperor")
    with pytest.raises(handler.Rejected, match="no declared role"):
        handler._gateway_session(_GatewayContext("t"), period="2026", session_id="req-000001")


def test_a_call_that_did_not_come_through_a_gateway_builds_no_gateway_session(repo_root) -> None:
    """The runtime's HTTP surface still authenticates from verified, forwarded claims."""
    assert handler._gateway_session(None, period="2026", session_id="req-000001") is None
    assert (
        handler._gateway_session(_GatewayContext("t", gateway=""), period="2026", session_id="r-1")
        is None
    )


def test_a_gateway_invocation_builds_its_session_without_any_claims(repo_root, monkeypatch):
    """The wiring, which was defined and then not called.

    `_gateway_session` existed, was tested, and `invoke` never reached it: an edit had been
    written against a two-line form that ruff had already collapsed into one, so the
    replacement silently matched nothing. The estate went on answering `unknown tenant ''`
    while every unit test around the helper passed.

    This asserts the path, not the helper. A gateway invocation carries no claims at all, so
    reaching authorization at all means the session was built from the gateway.
    """
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("ATTESTOR_SURFACE_ROLE_HELIOS", "role:preparer")

    response = handler.invoke(
        {"datapoint_id": "ESRS_E1-6_gross_scope_1"},
        _GatewayContext("attestor-tools___read_lineage"),
    )

    assert "unknown tenant" not in str(response["body"]), response["body"]


def test_the_runtime_takes_its_tenant_from_the_resource(repo_root, monkeypatch) -> None:
    """One runtime per tenant, the same as one gateway per tenant, and for the same reason.

    A JWT authorizer validates against exactly one issuer. The single shared runtime that this
    replaces pointed its `discovery_url` at `values(...)[0]` — an arbitrary map ordering — while
    listing every tenant's client, so one tenant could reach it, the rest could not, and which
    one was decided by iteration order. A runtime has no client context to read an identity out
    of, so the fact is set on the resource where no caller can reach it.
    """
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("ATTESTOR_TENANT", "aegis")
    monkeypatch.setenv("ATTESTOR_SURFACE_ROLE_AEGIS", "role:preparer")

    session = handler._runtime_session(period="2026", session_id="req-000001")

    assert session.tenant == "aegis"
    assert session.roles == frozenset({"role:preparer"})
    assert session.subject == "runtime:aegis"


def test_off_the_runtime_path_there_is_no_runtime_session(repo_root, monkeypatch) -> None:
    monkeypatch.delenv("ATTESTOR_TENANT", raising=False)
    assert handler._runtime_session(period="2026", session_id="req-000001") is None


def test_a_runtime_serving_an_unknown_tenant_runs_nothing(repo_root, monkeypatch) -> None:
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("ATTESTOR_TENANT", "somebody-else")
    with pytest.raises(handler.Rejected, match="not a tenant"):
        handler._runtime_session(period="2026", session_id="req-000001")


def test_an_invocation_with_no_identity_at_all_is_refused(repo_root, monkeypatch) -> None:
    """The safe state is no output. Not a default tenant, not a default role — nothing."""
    monkeypatch.chdir(repo_root)
    monkeypatch.delenv("ATTESTOR_TENANT", raising=False)

    response = handler.invoke(
        {"tool": "read_lineage", "arguments": {"datapoint_id": "ESRS_E1-6_gross_scope_1"}}, None
    )

    assert response["statusCode"] >= 400
    assert "nothing here says who is calling" in str(response["body"])
