"""The Lambda entry point AgentCore Gateway invokes.

It does four things, in this order, and the order is the security model:

1. **Builds a session from the verified token claims.** The Gateway has already checked the
   signature, issuer, audience and expiry; what happens here is mapping *this tenant's*
   provider group names onto roles. If no group maps, no session exists and nothing runs.
2. **Rejects any argument that names a scope.** The OpenAPI schema has no `tenant` property,
   so an argument carrying one arrived from somewhere it should not have. That is refused
   rather than ignored — ignoring it makes the attempt invisible in a log.
3. **Dispatches to a `Toolbox` bound to that session**, which authorizes against Cedar before
   doing any work.
4. **Emits a structured line per invocation.** `tenant_id` and `session_id` on every record,
   because a latency graph without them is one line for three customers and the cost meter
   has nothing to attribute a charge to.

There is no path through this function that reaches a tool without a session, and no path
that lets a caller choose the session it gets.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from attestor.agent import memory, narrative
from attestor.agent.tools import SPECS, Denied, Toolbox
from attestor.contracts import loader, overrides
from attestor.contracts.model import Standard
from attestor.datapoints.backends import AthenaBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import Resolver
from attestor.policy import cedar
from attestor.policy.tenants import Session, TenantRegistry, UnknownRole, WrongIssuer

LOG = logging.getLogger("attestor.agent")
LOG.setLevel(logging.INFO)

#: Argument names that would let a caller pick its own scope. The schema forbids them; this
#: is the second refusal, because a schema is enforced by whatever validates it.
FORBIDDEN_ARGUMENTS = frozenset(
    {"tenant", "tenant_id", "period", "principal", "session", "session_id", "role", "roles"}
)

ROOT = Path(os.environ.get("ATTESTOR_ROOT", "/var/task"))


class Rejected(ValueError):
    """The invocation is malformed in a way that matters. Never softened into a default."""


def _emit(event: str, **fields: Any) -> None:
    LOG.info(json.dumps({"event": event, **fields}, default=str))


# The declarative material is immutable for the life of the container: contracts, tenants,
# the Cedar policies and the override register all ship inside the image, so re-reading and
# re-validating them on every invocation buys nothing and costs a cold start's worth of work
# on every warm one. It is cached here rather than at each call site so there is one place
# where "this cannot change while the process lives" is asserted.
#
# Nothing tenant-scoped is cached. The evidence index, the resolver and the toolbox are built
# per session, because a cache that outlived a session is exactly the shape of a leak.
class _Declarative:
    def __init__(self) -> None:
        self.contracts = loader.load(ROOT)
        self.registry = TenantRegistry.load(ROOT)
        self.policies = cedar.load(ROOT)
        self.overrides = overrides.load_register(ROOT)


_DECLARATIVE: _Declarative | None = None


def declarative() -> _Declarative:
    global _DECLARATIVE  # noqa: PLW0603 — process-lifetime cache of immutable material
    if _DECLARATIVE is None:
        _DECLARATIVE = _Declarative()
    return _DECLARATIVE


def reset_cache() -> None:
    """Drop the process cache. For tests that rewrite `ATTESTOR_ROOT` between cases."""
    global _DECLARATIVE  # noqa: PLW0603
    _DECLARATIVE = None


def build_session(claims: dict[str, Any], *, tenant_id: str, period: str, session_id: str):
    tenant = declarative().registry.get(tenant_id)
    if tenant is None:
        raise Rejected(f"unknown tenant {tenant_id!r}")
    return Session.from_claims(claims, tenant=tenant, period=period, session_id=session_id)


def build_toolbox(session: Session) -> Toolbox:
    """Wire a toolbox for one session. Every collaborator is scoped by it."""
    shared = declarative()
    standard = Standard(shared.registry[session.tenant].standard)
    for_tenant = shared.contracts.for_standard(standard)

    backend = AthenaBackend(
        workgroup=os.environ["ATTESTOR_WORKGROUP"],
        catalog=os.environ.get("ATTESTOR_CATALOG", "AwsDataCatalog"),
        database=os.environ.get("ATTESTOR_DATABASE", "attestor_gold"),
        output_location=os.environ["ATTESTOR_ATHENA_OUTPUT"],
        region=os.environ.get("AWS_REGION", "eu-central-1"),
    )
    resolver = Resolver(
        contracts=for_tenant,
        backend=backend,
        evidence=EvidenceIndex.for_tenant(ROOT, session.tenant),
        override_register=shared.overrides,
        root=ROOT,
        # Without this a narrative datapoint abstained with E_METHOD_UNAVAILABLE — an
        # internal failure — so every ESRS tenant blocked on a control that was working
        # exactly as designed against a provider nobody had connected.
        narrative_provider=narrative.build(ROOT, session=session, backend="athena"),
    )
    return Toolbox(
        session=session,
        policies=shared.policies,
        contracts=for_tenant,
        resolver=resolver,
        overrides=shared.overrides,
        retrieval=_retrieval(),
    )


def _retrieval():
    from attestor.agent.bedrock import BedrockRetrieval  # noqa: PLC0415 — needs boto3

    return BedrockRetrieval(
        region=os.environ.get("AWS_REGION", "eu-central-1"),
        evidence_kb_id=os.environ["ATTESTOR_EVIDENCE_KB"],
        regulatory_kb_id=os.environ["ATTESTOR_REGULATORY_KB"],
    )


def _remember_refusal(
    event: dict[str, Any], request_id: str, tool: str, outcome: str, detail: str
) -> None:
    """Record a refusal, if a session can be built at all.

    A refusal is the more interesting half of the history. "Cedar denied `request_override`"
    is precisely what an analyst needs to see when they ask why nothing happened, and it is
    what an auditor asks about later.

    Rebuilt rather than reused because the refusal may have happened before the session
    existed — and where it did, there is nothing to attribute the event to and nothing is
    written. A memory write is never a reason to construct a session that authorization
    refused to construct.
    """
    try:
        session = build_session(
            dict(event.get("claims") or {}),
            tenant_id=str(event.get("tenant_id", "")),
            period=str(event.get("period", "")),
            session_id=request_id,
        )
    except Exception:  # no session, nothing to attribute
        return
    memory.record_invocation(session, tool=tool, outcome=outcome, detail=detail)


def invoke(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Gateway → tool. Returns a JSON body; never raises past this boundary."""
    started = dt.datetime.now(dt.UTC)
    tool = str(event.get("tool") or event.get("operationId") or "")
    arguments = dict(event.get("arguments") or {})
    claims = dict(event.get("claims") or {})
    # Not "local". A `Session` requires at least six characters, so the old default could not
    # construct one — and every invocation arriving without a Lambda context died as a 500
    # with a pydantic message, which reads as a broken tool rather than a missing correlation
    # id. The default is now valid *and* obviously a default.
    request_id = getattr(context, "aws_request_id", None) or "local-no-request-id"

    try:
        smuggled = sorted(set(arguments) & FORBIDDEN_ARGUMENTS)
        if smuggled:
            # The schema has no such properties, so these did not come from a well-formed
            # call. Refusing is what makes the attempt appear in CloudWatch.
            raise Rejected(
                f"arguments {', '.join(smuggled)} name a scope; scope comes from the session"
            )

        # The call is checked for shape before anything is built. Constructing a toolbox
        # opens an Athena client and a Bedrock provider; doing that for a tool that does not
        # exist means a malformed call is answered by whichever dependency fails first, and
        # `unknown tool` arrives as a 500 about an unset environment variable.
        spec = next((s for s in SPECS if s.name == tool), None)
        if spec is None:
            raise Rejected(f"unknown tool {tool!r}")

        # The schema declares `additionalProperties: false`, but a schema is enforced by
        # whatever validates it. An argument outside the spec would otherwise reach the
        # handler as an unexpected keyword and surface as a 500 — an internal error for
        # what is plainly a malformed call.
        unexpected = sorted(set(arguments) - set(spec.parameters))
        if unexpected:
            raise Rejected(f"{tool} does not accept argument(s) {', '.join(unexpected)}")
        missing = sorted(set(spec.required) - set(arguments))
        if missing:
            raise Rejected(f"{tool} requires argument(s) {', '.join(missing)}")

        session = build_session(
            claims,
            tenant_id=str(event.get("tenant_id", "")),
            period=str(event.get("period", "")),
            session_id=request_id,
        )
        toolbox = build_toolbox(session)
        handler = getattr(toolbox, tool, None)
        if handler is None:  # pragma: no cover — SPECS and Toolbox are cross-checked in tests
            raise Rejected(f"unknown tool {tool!r}")

        result = handler(**arguments)
        _emit(
            "tool.invoked",
            tool=tool,
            tenant=session.tenant,
            session_id=session.session_id,
            ms=_elapsed(started),
            # Whether the question was remembered, not whether it was answered. An analyst
            # asks follow-ups — "why is Scope 1 not disclosed" then "what would unblock it" —
            # and the second is only answerable if the first was recorded. It is written
            # after the answer exists and never gates it: memory is continuity, and the
            # doctrine fails open on continuity the way it fails closed on a guardrail.
            remembered=memory.record_invocation(
                session,
                tool=tool,
                outcome="ok",
                detail=json.dumps(result, default=str),
            ),
        )
        return {"statusCode": 200, "body": result}

    except Denied as denied:
        _emit(
            "authorization.denied",
            action=denied.action,
            tenant=str(event.get("tenant_id", "")),
            session_id=request_id,
            reason=denied.decision.reason,
            policies=list(denied.decision.determining),
        )
        _remember_refusal(event, request_id, tool, "denied", str(denied))
        return {"statusCode": 403, "body": {"error": str(denied)}}

    except WrongIssuer as wrong:
        # Not a 400. A well-formed call presenting a token from another tenant's provider is
        # an authorization event, and it belongs on the same metric filter as a Cedar denial
        # so that probing shows up as a spike rather than as scattered client errors.
        _emit(
            "authorization.denied",
            action=tool,
            tenant=str(event.get("tenant_id", "")),
            session_id=request_id,
            reason=str(wrong),
            policies=["tenant-issuer-binding"],
        )
        return {"statusCode": 403, "body": {"error": "the token does not serve this tenant"}}

    except (Rejected, UnknownRole) as rejected:
        _emit(
            "invocation.rejected",
            tool=tool,
            tenant=str(event.get("tenant_id", "")),
            session_id=request_id,
            reason=str(rejected),
        )
        return {"statusCode": 400, "body": {"error": str(rejected)}}

    except Exception as exc:
        # The message is logged in full and *not* returned. An internal error message is an
        # excellent map of the system for whoever provoked it.
        _emit(
            "invocation.failed",
            tool=tool,
            tenant=str(event.get("tenant_id", "")),
            session_id=request_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"statusCode": 500, "body": {"error": "the tool failed", "request_id": request_id}}


def _elapsed(started: dt.datetime) -> int:
    return int((dt.datetime.now(dt.UTC) - started).total_seconds() * 1000)
