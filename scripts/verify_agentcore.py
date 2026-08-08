#!/usr/bin/env python3
"""Call the deployed AgentCore surface with a real token and check what it does.

Nothing had ever called it. The gateway, the runtime, the six MCP tools, the Cedar `forbid` at
the edge and two per-tenant memories were deployed, `READY`, correctly configured, bound to the
right ARNs — and had served zero requests, from CI, from a script, from anything. `grep` for an
invocation across the repository returned nothing.

That is not a gap in testing. It hid a defect that made the whole path unusable: `tenants/*.yaml`
declared `eu-central-1_EXAMPLE` as the Cognito issuer, nothing substituted it, and the handler
compares a token's `iss` against it — so the first real token would have been refused by our own
code. A control nobody exercises is a control nobody can vouch for, and this one was broken.

Four questions, in the order they stop being answerable if the previous one fails:

1. Can a real tenant principal get a token at all?
2. Does a permitted tool work through the gateway, and return only that tenant's data?
3. Is `request_override` refused **by Cedar, at the edge** — the doctrine's one unopenable door?
4. Is a helios token refused at the aegis gateway — claim 2, at the identity boundary?

Needs credentials. Read-only against the estate: it authenticates, calls tools, and asserts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field

AWS = shutil.which("aws") or "aws"
#: Longer than the Lambda's own 180s, on purpose. A client that gives up before the server does
#: turns a slow answer into a traceback here and leaves the real outcome unknown — which is what
#: happened at 90: the tool was still resolving through Athena and this raised instead of
#: waiting. The server's limit is the one that should decide.
TIMEOUT_SECONDS = 240

#: The gateway target every tool is namespaced under. AgentCore's separator, three
#: underscores, not ours.
TARGET = "attestor-tools___"

#: What "it worked" and "it was refused" look like on the wire. A Cedar refusal at the edge is
#: a 403 the Lambda never sees, which is a stronger statement than a tool returning an error.
HTTP_OK = 200
REFUSED = frozenset({401, 403})


@dataclass
class Report:
    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        """Record it and say it, now.

        Printed as it happens rather than collected for a summary. A probe that reports at the
        end reports nothing when it dies in the middle — which is exactly what happened: a CLI
        that had never heard of `bedrock-agentcore` raised in the last check and took five
        passing assertions with it, including the two this project most wanted evidence for.
        The summary still prints; it is no longer the only thing that does.
        """
        self.results.append((name, ok, detail))
        print(
            f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""), flush=True
        )

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


def aws(*args: str) -> str:
    """One AWS CLI call. Raises, so a broken probe cannot read as a pass."""
    return subprocess.run(  # noqa: S603
        [AWS, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def aws_or_none(*args: str) -> str | None:
    """The same call, for a probe whose failure is a finding rather than an abort.

    The memory probes run last and had been raising. A CLI on the runner that did not know
    `bedrock-agentcore` therefore ended the whole script with a traceback — discarding five
    assertions that had already passed, including the two this project cares most about. A
    verification that reports nothing when its last check errors is worse than one that has no
    last check.
    """
    result = subprocess.run([AWS, *args], capture_output=True, text=True, check=False)  # noqa: S603
    return result.stdout.strip() if result.returncode == 0 else None


def token_for(tenant: str, secret_name: str, region: str) -> str:
    """Authenticate as this tenant's verification principal and return its access token.

    `ADMIN_USER_PASSWORD_AUTH` against a user in a real group, not a client-credentials grant.
    Only a user token carries `cognito:groups`, which is what the handler maps onto a role —
    a machine token would exercise a code path no person ever takes.

    Through the tenant's *own* app client, the one its people sign in with. That costs the
    SECRET_HASH below and buys two things: the token carries the same `aud` a person's does,
    and the gateway's authorizer never has to list a second client. Listing one is an update
    to the gateway, and Cloud Control answers an update by sending the whole authorizer back
    with `AllowedAudience: []`, which the model rejects.
    """
    secret = json.loads(
        aws(
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            secret_name,
            "--query",
            "SecretString",
            "--output",
            "text",
        )
    )
    secret_hash = base64.b64encode(
        hmac.new(
            secret["client_secret"].encode(),
            (secret["username"] + secret["client_id"]).encode(),
            hashlib.sha256,
        ).digest()
    ).decode()

    response = json.loads(
        aws(
            "cognito-idp",
            "admin-initiate-auth",
            "--region",
            region,
            "--user-pool-id",
            secret["pool_id"],
            "--client-id",
            secret["client_id"],
            "--auth-flow",
            "ADMIN_USER_PASSWORD_AUTH",
            "--auth-parameters",
            f"USERNAME={secret['username']},PASSWORD={secret['password']},"
            f"SECRET_HASH={secret_hash}",
            "--output",
            "json",
        )
    )
    return str(response["AuthenticationResult"]["AccessToken"])


class Mcp:
    """One MCP session against a gateway, over streamable HTTP.

    Four things had to be right and every one of them was learned by being told off:

    - the endpoint is `<gateway_url>/mcp`; the bare URL answers `UnknownOperationException`
      inside an HTTP 200, which is the most misleading success code available
    - `MCP-Protocol-Version: 2025-06-18`; the client default of `2025-03-26` is refused
    - `initialize` returns an `Mcp-Session-Id` header every later call must carry
    - and `notifications/initialized` must follow it, or the session is not usable

    The first version of this file skipped all of that, sent one `tools/call`, got HTTP 200 and
    reported a pass. The body said `UnknownOperationException`. A check that reads the status
    line and not the answer is the same vacuous pass this repository has now written three
    times in different files.
    """

    PROTOCOL = "2025-06-18"

    def __init__(self, gateway_url: str, token: str) -> None:
        self.url = gateway_url.rstrip("/") + "/mcp"
        self.token = token
        self.session: str | None = None

    def _post(self, payload: dict) -> tuple[int, dict, dict]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.PROTOCOL,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(  # noqa: S310 — https, from a Terraform output
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                return response.status, dict(response.headers), _parse(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), _parse(exc.read().decode() or "{}")
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            # Reported, never raised. A probe that dies on a slow call reports nothing at all —
            # including the checks that already passed — and this file has now been corrected
            # for that twice. The status is one nothing else returns, so it cannot be confused
            # for an answer the gateway gave.
            return 0, {}, {"error": f"no answer from the gateway: {exc}"}

    def open(self) -> tuple[int, dict]:
        status, headers, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": self.PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "attestor-verification", "version": "1"},
                },
            }
        )
        self.session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if self.session:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return status, body

    def tools(self) -> list[str]:
        _, _, body = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return [t["name"] for t in body.get("result", {}).get("tools", [])]

    def call(self, tool: str, arguments: dict) -> tuple[int, dict]:
        """`<target>___<tool>` — the Gateway qualifies every tool with its target's name."""
        status, _, body = self._post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": f"{TARGET}{tool}", "arguments": arguments},
            }
        )
        return status, body


def _parse(raw: str) -> dict:
    """MCP over HTTP may answer as JSON or as a single SSE frame."""
    text = raw.strip()
    if text.startswith("event:") or text.startswith("data:"):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[len("data:") :].strip()
                break
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError:
        return {"raw": text[:400]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--gateways", required=True, help='JSON {"helios": url, "aegis": url}')
    parser.add_argument("--secrets", required=True, help='JSON {"helios": name, "aegis": name}')
    parser.add_argument("--memories", default="{}", help='JSON {"helios": memory id}')
    arguments = parser.parse_args()

    gateways = json.loads(arguments.gateways)
    secrets = json.loads(arguments.secrets)
    report = Report()

    # 1. A token at all. Everything below is unanswerable without one, which is precisely the
    #    state the estate was in: no domain, no users, no way to authenticate as anybody.
    try:
        helios_token = token_for("helios", secrets["helios"], arguments.region)
        report.check("helios: the verification principal can authenticate", True, "token issued")
    except Exception as exc:  # the message is the finding
        report.check("helios: the verification principal can authenticate", False, str(exc)[:200])
        _print(report)
        return 1

    session = Mcp(gateways["helios"], helios_token)
    status, body = session.open()
    report.check(
        "helios: an MCP session opens on the gateway",
        status == HTTP_OK and bool(session.session),
        f"HTTP {status} session={session.session}",
    )

    # 2. The catalogue. Cedar filters `tools/list` as well as `tools/call`, so a forbidden tool
    #    is not merely refused when called — it is never offered. Five names, not six.
    offered = session.tools()
    report.check(
        "the gateway offers the tools, and not the forbidden one",
        bool(offered) and f"{TARGET}request_override" not in offered,
        f"{len(offered)} tool(s): {[name.removeprefix(TARGET) for name in offered]}",
    )

    # 3. A permitted tool, end to end: JWT verified at the edge, Cedar consulted, session built
    #    from claims in the Lambda, Athena queried, answer returned. `isError` and the body are
    #    both read — the first version of this check looked at the HTTP status alone and passed
    #    on `{"__type": "UnknownOperationException"}` inside a 200.
    status, body = session.call("read_lineage", {"datapoint_id": "ESRS_E1-6_gross_scope_1"})
    result = body.get("result", {})
    answered = str(result.get("content", ""))
    report.check(
        "helios: a permitted tool answers through the gateway",
        status == HTTP_OK
        and not result.get("isError", True)
        and "error" not in answered
        and "lineage" in answered.lower(),
        f"HTTP {status} {json.dumps(body)[:260]}",
    )

    # 4. Doctrine rule 2, enforced rather than read. This had only ever been confirmed by
    #    opening the policy file.
    status, body = session.call(
        "request_override",
        {"datapoint_id": "ESRS_E1-6_gross_scope_1", "justification": "verification probe"},
    )
    denial = json.dumps(body).lower()
    report.check(
        "the override door is shut at the edge, not just in the policy file",
        "policy" in denial and ("denied" in denial or "not allowed" in denial),
        f"HTTP {status} {json.dumps(body)[:260]}",
    )

    # 5. Claim 2 at the identity boundary. One pool per tenant means one issuer per tenant, and
    #    a JWT authorizer validates against one issuer's keys — so this fails before any policy
    #    runs, which is stronger than a Cedar condition and the reason it is built that way.
    if "aegis" in gateways:
        foreign = Mcp(gateways["aegis"], helios_token)
        status, body = foreign.open()
        report.check(
            "a helios token opens nothing at the aegis gateway",
            status in REFUSED,
            f"HTTP {status} {json.dumps(body)[:200]}",
        )

    memories = json.loads(arguments.memories)
    if memories.get("helios"):
        # `list_events` needs an actor and a session, and the session id is the Lambda's request
        # id — not knowable from out here. So this walks down: which actors has this memory ever
        # heard from, and did any of them leave an event. An empty actor list is a specific
        # finding, not an absence of one: nothing has ever written here.
        #
        # Tolerant calls, because these run last. When they raised, a runner CLI that did not
        # know `bedrock-agentcore` ended the script with a traceback and discarded five
        # assertions that had already passed.
        raw_actors = aws_or_none(
            "bedrock-agentcore",
            "list-actors",
            "--region",
            arguments.region,
            "--memory-id",
            memories["helios"],
            "--query",
            "actorSummaries[].actorId",
            "--output",
            "json",
        )
        if raw_actors is None:
            report.check(
                "helios: this tenant's memory can be read at all",
                False,
                "bedrock-agentcore list-actors failed; the data plane is unreachable from here",
            )
        else:
            actors = json.loads(raw_actors or "[]")
            report.check(
                "helios: something has written to this tenant's memory",
                bool(actors),
                f"actor(s) {actors[:3]}" if actors else "no actor has written; memory is inert",
            )

            events: list = []
            for actor in actors[:3]:
                raw_sessions = aws_or_none(
                    "bedrock-agentcore",
                    "list-sessions",
                    "--region",
                    arguments.region,
                    "--memory-id",
                    memories["helios"],
                    "--actor-id",
                    actor,
                    "--query",
                    "sessionSummaries[].sessionId",
                    "--output",
                    "json",
                )
                for session_id in json.loads(raw_sessions or "[]")[:3]:
                    raw_events = aws_or_none(
                        "bedrock-agentcore",
                        "list-events",
                        "--region",
                        arguments.region,
                        "--memory-id",
                        memories["helios"],
                        "--actor-id",
                        actor,
                        "--session-id",
                        session_id,
                        "--max-results",
                        "10",
                        "--query",
                        "events[].eventId",
                        "--output",
                        "json",
                    )
                    events += json.loads(raw_events or "[]")

            if actors:
                report.check(
                    "helios: the tool invocation itself was recorded",
                    bool(events),
                    f"{len(events)} event(s)" if events else "an actor exists but left no event",
                )

    _print(report)
    return 1 if report.failed else 0


def _print(report: Report) -> None:
    passed = len(report.results) - len(report.failed)
    print(f"\n  {passed}/{len(report.results)} passed")


if __name__ == "__main__":
    raise SystemExit(main())
