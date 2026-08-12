"""The HTTP surface AgentCore Runtime reaches the container through.

This module was at 0% coverage. It is the `CMD` of the Dockerfile — the code AWS actually
executes — and the only thing that had ever run it was `scripts/verify_agentcore.py`, against
a live estate. That contradicts two rules this repository states plainly: offline is the
default, and generated-but-unrun code is not done. A `ThreadingHTTPServer` with two routes
needs no account to test; it needs a socket.

The server is driven here exactly as Runtime drives it: a real socket, a real HTTP client,
real headers. `tool_handler.invoke` is stubbed, because what is under test is the socket —
the handler behind it has its own tests, and wiring a live one in would need Athena, Bedrock
and a knowledge base to answer a question about routing.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from attestor.agent import server as server_module

CLAIMS_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Claims"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


@pytest.fixture
def seen() -> list[tuple[dict[str, Any], Any]]:
    return []


@pytest.fixture
def running(monkeypatch: pytest.MonkeyPatch, seen) -> Iterator[HTTPConnection]:
    """The real server on an ephemeral port, with the tool handler stubbed."""

    def _invoke(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        seen.append((event, context))
        return {"statusCode": 200, "body": {"ok": True}}

    monkeypatch.setattr(server_module.tool_handler, "invoke", _invoke)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    connection = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    try:
        yield connection
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(connection: HTTPConnection, path: str, body: bytes, **headers: str):
    connection.request(
        "POST", path, body=body, headers={"Content-Length": str(len(body)), **headers}
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read() or b"{}")


# ── Liveness ─────────────────────────────────────────────────────────────────


def test_ping_answers_without_touching_the_handler(running, seen) -> None:
    """Health checks run constantly. One that resolved a datapoint would bill for liveness."""
    running.request("GET", "/ping")
    response = running.getresponse()
    assert response.status == 200
    assert json.loads(response.read())["status"] == "Healthy"
    assert seen == []


def test_ping_tolerates_a_trailing_slash(running) -> None:
    running.request("GET", "/ping/")
    assert running.getresponse().status == 200


def test_an_unknown_path_is_a_404_on_both_verbs(running) -> None:
    running.request("GET", "/metrics")
    response = running.getresponse()
    response.read()  # keep-alive: the body must be drained before the next request
    assert response.status == 404
    status, _ = _post(running, "/invoke", b"{}")
    assert status == 404


# ── The invocation body ──────────────────────────────────────────────────────


def test_an_invocation_reaches_the_handler_with_its_arguments(running, seen) -> None:
    status, body = _post(
        running,
        "/invocations",
        json.dumps(
            {
                "tool": "read_lineage",
                "arguments": {"datapoint_id": "ESRS_E1-6_gross_scope_1"},
                "tenant_id": "helios",
                "period": "2026",
            }
        ).encode(),
    )
    assert status == 200
    assert body == {"ok": True}
    event, _ = seen[0]
    assert event["tool"] == "read_lineage"
    assert event["arguments"] == {"datapoint_id": "ESRS_E1-6_gross_scope_1"}
    assert event["tenant_id"] == "helios"
    assert event["period"] == "2026"


def test_the_handlers_status_code_is_the_responses(running, monkeypatch) -> None:
    """A refusal is a refusal on the wire, not a 200 carrying an error object."""
    monkeypatch.setattr(
        server_module.tool_handler,
        "invoke",
        lambda event, context=None: {"statusCode": 403, "body": {"error": "denied"}},
    )
    status, body = _post(running, "/invocations", json.dumps({"tool": "resolve"}).encode())
    assert status == 403
    assert body["error"] == "denied"


def test_a_body_that_is_not_a_json_object_is_refused(running, seen) -> None:
    status, body = _post(running, "/invocations", b'["resolve_datapoint"]')
    assert status == 400
    assert "JSON object" in body["error"]
    assert seen == []


def test_malformed_json_is_a_400_not_a_500(running, seen) -> None:
    status, _ = _post(running, "/invocations", b"{not json")
    assert status == 400
    assert seen == []


def test_an_oversized_body_is_refused_by_its_declared_length(running, seen) -> None:
    """Refused on Content-Length, before the bytes are read into the container's memory."""
    oversized = b'{"tool":"x","pad":"' + b"a" * (server_module.MAX_BODY_BYTES + 10) + b'"}'
    status, body = _post(running, "/invocations", oversized)
    assert status == 400
    assert "ceiling" in body["error"]
    assert seen == []


def test_an_empty_body_is_still_a_well_formed_invocation(running, seen) -> None:
    """It reaches the handler and is refused there, where 'unknown tool' is said properly."""
    status, _ = _post(running, "/invocations", b"")
    assert status == 200
    event, _ = seen[0]
    assert event["tool"] is None
    assert event["arguments"] == {}


# ── Identity ─────────────────────────────────────────────────────────────────


def test_verified_claims_are_forwarded_from_the_header(running, seen) -> None:
    claims = {"iss": "https://issuer.example/helios", "aud": "attestor-helios", "sub": "u-1"}
    _post(
        running,
        "/invocations",
        json.dumps({"tool": "read_lineage", "tenant_id": "helios"}).encode(),
        **{CLAIMS_HEADER: json.dumps(claims)},
    )
    event, _ = seen[0]
    assert event["claims"] == claims


def test_an_unparseable_claims_header_yields_no_claims_rather_than_a_crash(running, seen) -> None:
    """No claims is a state the handler already knows how to refuse. A 500 is not."""
    status, _ = _post(
        running,
        "/invocations",
        json.dumps({"tool": "read_lineage"}).encode(),
        **{CLAIMS_HEADER: "not-json"},
    )
    assert status == 200
    event, _ = seen[0]
    assert event["claims"] == {}


def test_a_claims_header_that_is_not_an_object_yields_no_claims(running, seen) -> None:
    _post(
        running,
        "/invocations",
        json.dumps({"tool": "read_lineage"}).encode(),
        **{CLAIMS_HEADER: '["helios"]'},
    )
    event, _ = seen[0]
    assert event["claims"] == {}


def test_the_session_id_comes_from_the_runtime_header(running, seen) -> None:
    _post(
        running,
        "/invocations",
        json.dumps({"tool": "read_lineage"}).encode(),
        **{SESSION_HEADER: "session-from-agentcore"},
    )
    _, context = seen[0]
    assert context.aws_request_id == "session-from-agentcore"


def test_a_missing_session_header_still_constructs_a_session(running, seen) -> None:
    """`Session` requires at least six characters, so the fallback has to be a real one —
    a shorter default used to surface as an internal error about a pydantic constraint."""
    _post(running, "/invocations", json.dumps({"tool": "read_lineage"}).encode())
    _, context = seen[0]
    assert len(context.aws_request_id) >= 6


# ── Logging ──────────────────────────────────────────────────────────────────


def test_the_access_log_never_carries_the_request_line(running, caplog) -> None:
    """A path can carry a tenant or a datapoint id, and access logs are the classic
    accidental data export. This assertion is the reason `log_request` exists: routing the
    access log through `log_message` put `"POST /invocations HTTP/1.1"` in every line while
    the docstring above it claimed the opposite."""
    with caplog.at_level("INFO", logger="attestor.agent.server"):
        _post(
            running,
            "/invocations",
            json.dumps({"tool": "read_lineage", "tenant_id": "helios"}).encode(),
        )
    assert "helios" not in caplog.text
    assert "/invocations" not in caplog.text
    assert '"method": "POST"' in caplog.text
    assert '"status": "200"' in caplog.text
