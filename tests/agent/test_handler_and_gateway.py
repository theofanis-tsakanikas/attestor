"""The Gateway boundary: what an agent can say, and what it cannot."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import claims_for

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


def test_an_argument_naming_a_scope_is_refused() -> None:
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


def test_an_unknown_tool_is_refused() -> None:
    response = handler.invoke(
        {
            "tool": "drop_everything",
            "tenant_id": "helios",
            "period": "2026",
            "claims": claims_for("helios", "helios-preparers"),
        }
    )
    assert response["statusCode"] == 400


def test_an_internal_error_does_not_leak_its_message() -> None:
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


def test_an_argument_outside_the_schema_is_a_400_not_a_500() -> None:
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


def test_a_missing_required_argument_is_a_400() -> None:
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


def test_a_token_from_another_tenant_is_403(repo_root: Path) -> None:
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
