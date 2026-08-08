"""AgentCore Memory: that it is written, that it is scoped, and that it cannot break a read.

The memory resource was deployed for weeks with `encryption_key_arn`, a retention period and
a name per tenant, and not one line of code referenced it. `grep -rn memory src/attestor/agent`
returned a comment about containers running out of it. That is the failure mode this file
exists to make impossible: a capability that is configured, paid for, and inert.
"""

from __future__ import annotations

import datetime as dt

import pytest

from attestor.agent import memory
from attestor.policy.tenants import Session

FROZEN = dt.datetime(2026, 8, 8, 9, 0, tzinfo=dt.UTC)


class StubMemory:
    """Records calls. Raises on demand, because failing is the interesting behaviour."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def create_event(self, **kwargs):
        if self.fail:
            raise RuntimeError("throttled")
        self.calls.append(kwargs)
        return {"event": {"eventId": "e-1"}}

    def list_events(self, **kwargs):
        if self.fail:
            raise RuntimeError("throttled")
        self.calls.append(kwargs)
        return {"events": [{"eventId": "e-1"}]}


def _session(tenant: str = "helios") -> Session:
    return Session(
        tenant=tenant,
        subject="analyst-1",
        roles=frozenset({"role:preparer"}),
        period="2026",
        session_id="sess-000001",
    )


def test_an_invocation_is_written_to_this_tenants_memory(monkeypatch) -> None:
    monkeypatch.setenv("ATTESTOR_MEMORY_HELIOS", "mem-helios")
    client = StubMemory()

    written = memory.record_invocation(
        _session(), tool="read_lineage", outcome="ok", detail="{}", client=client, now=FROZEN
    )

    assert written
    assert client.calls[0]["memoryId"] == "mem-helios"
    assert client.calls[0]["sessionId"] == "sess-000001"
    assert client.calls[0]["actorId"] == "analyst-1"


def test_the_memory_is_chosen_by_tenant_and_nothing_else(monkeypatch) -> None:
    """Probe 9 of the isolation suite, given something live to be about.

    A shared memory with a tenant column would put the boundary in a filter, and a filter is
    something one query can forget. This is a different resource id per tenant, resolved from
    the session, so forgetting it is not a thing the code can do.
    """
    monkeypatch.setenv("ATTESTOR_MEMORY_HELIOS", "mem-helios")
    monkeypatch.setenv("ATTESTOR_MEMORY_AEGIS", "mem-aegis")
    client = StubMemory()

    memory.record_invocation(_session("helios"), tool="t", outcome="ok", client=client)
    memory.record_invocation(_session("aegis"), tool="t", outcome="ok", client=client)

    assert [c["memoryId"] for c in client.calls] == ["mem-helios", "mem-aegis"]


def test_the_namespace_is_derived_never_supplied() -> None:
    assert memory.namespace(_session("aegis")) == "attestor/aegis/sess-000001"
    assert memory.namespace(_session("helios")) == "attestor/helios/sess-000001"


def test_a_memory_failure_does_not_reach_the_caller(monkeypatch, caplog) -> None:
    """Fail open, loudly. The doctrine's other half.

    A guardrail error means no output. A memory error means the analyst still gets their
    answer and somebody is told the history is not being kept — because no tool reads memory
    to authorize, to resolve or to abstain, so a lost event cannot change a single number.
    """
    monkeypatch.setenv("ATTESTOR_MEMORY_HELIOS", "mem-helios")

    assert (
        memory.record_invocation(_session(), tool="t", outcome="ok", client=StubMemory(fail=True))
        is False
    )
    assert "memory not recorded" in caplog.text


def test_an_unconfigured_memory_is_reported_not_raised(monkeypatch, caplog) -> None:
    monkeypatch.delenv("ATTESTOR_MEMORY_HELIOS", raising=False)

    assert memory.record_invocation(_session(), tool="t", outcome="ok") is False
    assert "ATTESTOR_MEMORY_HELIOS is unset" in caplog.text


def test_reading_back_is_scoped_the_same_way(monkeypatch) -> None:
    monkeypatch.setenv("ATTESTOR_MEMORY_HELIOS", "mem-helios")
    client = StubMemory()

    assert memory.recent(_session(), limit=5, client=client)
    assert client.calls[0] == {
        "memoryId": "mem-helios",
        "actorId": "analyst-1",
        "sessionId": "sess-000001",
        "maxResults": 5,
    }


def test_a_long_result_is_truncated_before_it_is_remembered(monkeypatch) -> None:
    """A memory holds the shape of an answer, never the answer.

    The figure is re-derived from the contract and the lakehouse on every call, which is the
    only reason remembering anything about it is safe. Storing the whole result would make
    the memory a second source for a number, and this project has exactly one.
    """
    monkeypatch.setenv("ATTESTOR_MEMORY_HELIOS", "mem-helios")
    client = StubMemory()

    memory.record_invocation(_session(), tool="t", outcome="ok", detail="x" * 5000, client=client)

    text = client.calls[0]["payload"][0]["conversational"]["content"]["text"]
    assert len(text) < 5000
    assert '"detail": "' + "x" * memory.SUMMARY_LIMIT + '"' in text


@pytest.mark.parametrize("tenant", ["helios", "aegis", "lumen"])
def test_the_environment_variable_is_the_one_terraform_sets(monkeypatch, tenant) -> None:
    """The name is a contract between a Python module and a Terraform local.

    They are in different files, different languages, and nothing but this test connects
    them. `scripts/check_agentcore_wiring.py` asserts the other direction — that Terraform
    sets one for every tenant it creates a memory for.
    """
    monkeypatch.setenv(f"ATTESTOR_MEMORY_{tenant.upper()}", f"mem-{tenant}")
    assert memory.memory_id(tenant) == f"mem-{tenant}"
