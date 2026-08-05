"""The HTTP contract AgentCore Runtime expects: `/ping` and `/invocations`.

Runtime hosts a container and speaks to it over two endpoints. That is the whole interface,
and keeping it this thin is the point of the architecture: the agent's behaviour — which
datapoint to resolve, what a narrative may say, when to abstain — lives in the library and is
fully tested without an account. This module is the socket it is reached through.

The consequence worth stating: if AgentCore stops fitting, what moves is one file. The tools
are plain handlers behind a generated OpenAPI description, the policy is Cedar in a file, and
the orchestration is a resolver. None of them import an AgentCore SDK.

Deliberately stdlib. A framework here would add a dependency to the container, a version to
track and a CVE feed to watch, in exchange for routing two paths.

Two behaviours that are not obvious from the shape:

**`/ping` never touches the model or the lakehouse.** Health checks run constantly; a check
that resolves a datapoint would bill for liveness and would fail the container whenever
Athena was slow.

**A session is built per request from the caller's claims.** Runtime provides session
isolation between concurrent invocations, but isolation is not authorization — nothing here
trusts the runtime to have decided who is asking.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from attestor.agent import handler as tool_handler

LOG = logging.getLogger("attestor.agent.server")
LOG.setLevel(logging.INFO)

PORT = int(os.environ.get("PORT", "8080"))

#: A request larger than this is refused unread. The largest legitimate invocation is a
#: question and a few identifiers; anything else is either a mistake or an attempt to find
#: out what happens when the container runs out of memory.
MAX_BODY_BYTES = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── Plumbing ─────────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: Any) -> None:
        """Structured, and without the request line.

        The default writes the raw path to stderr, and a path can carry a datapoint id or a
        tenant. Access logs are the classic accidental data export.
        """
        LOG.info(json.dumps({"event": "http", "detail": fmt % args}))

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError(f"body of {length} bytes exceeds the {MAX_BODY_BYTES} ceiling")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("the invocation body must be a JSON object")
        return payload

    # ── Routes ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/ping":
            # Liveness only. It asserts the process is up, not that the estate is healthy —
            # conflating the two makes a slow query look like a crashed container.
            self._respond(200, {"status": "Healthy"})
            return
        self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/invocations":
            self._respond(404, {"error": "not found"})
            return
        try:
            payload = self._read()
        except (ValueError, json.JSONDecodeError) as exc:
            self._respond(400, {"error": str(exc)})
            return

        # AgentCore passes the verified JWT claims through this header. They are the only
        # source of identity: nothing in the body can name a tenant, a role or a principal,
        # and `tool_handler.invoke` refuses an argument that tries.
        claims = _claims(self.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Custom-Claims"))
        session_id = self.headers.get("X-Amzn-Bedrock-AgentCore-Runtime-Session-Id", "local")

        response = tool_handler.invoke(
            {
                "tool": payload.get("tool"),
                "arguments": payload.get("arguments", {}),
                "tenant_id": payload.get("tenant_id", ""),
                "period": payload.get("period", ""),
                "claims": claims,
            },
            _Context(session_id),
        )
        self._respond(int(response.get("statusCode", 500)), response.get("body", {}))


class _Context:
    """Mimics the Lambda context the tool handler already understands."""

    def __init__(self, request_id: str) -> None:
        self.aws_request_id = request_id


def _claims(header: str | None) -> dict[str, Any]:
    if not header:
        return {}
    try:
        parsed = json.loads(header)
    except json.JSONDecodeError:
        LOG.info(json.dumps({"event": "claims.unparseable"}))
        return {}
    return parsed if isinstance(parsed, dict) else {}


def serve(port: int = PORT) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 — a container port
    LOG.info(json.dumps({"event": "server.started", "port": port}))
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    serve()
