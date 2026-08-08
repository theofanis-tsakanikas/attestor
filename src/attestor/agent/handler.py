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
from attestor.policy.tenants import ROLES, Session, TenantRegistry, UnknownRole, WrongIssuer

LOG = logging.getLogger("attestor.agent")
LOG.setLevel(logging.INFO)

#: Argument names that would let a caller pick its own scope. The schema forbids them; this
#: is the second refusal, because a schema is enforced by whatever validates it.
FORBIDDEN_ARGUMENTS = frozenset(
    {"tenant", "tenant_id", "period", "principal", "session", "session_id", "role", "roles"}
)

ROOT = Path(os.environ.get("ATTESTOR_ROOT", "/var/task"))

#: The name every gateway is prefixed with, so `attestor-gateway-helios-kwrv5ursu9` can be read
#: back to `helios`. Terraform builds the name from the same value.
PROJECT = os.environ.get("ATTESTOR_PROJECT", "attestor")


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


#: How AgentCore Gateway names the tool it is invoking. Not in the event — in the Lambda client
#: context, prefixed with the gateway target: `attestor-tools___read_lineage`.
#:
#: This cost a live call to discover and could not have cost less. The handler read
#: `event["tool"]`, which is what our own tests send and what the runtime's HTTP surface sends,
#: and the Gateway sends neither — so every tool call through the gateway was answered
#: `unknown tool ''`. Offline the contract looked complete from both sides; the two sides had
#: simply never been introduced.
GATEWAY_TOOL_CONTEXT_KEY = "bedrockAgentCoreToolName"

#: The Gateway qualifies every tool with its target name. `attestor-tools___read_lineage` is
#: one tool, not a namespace to resolve — and the separator is three underscores, which is
#: AgentCore's, not ours.
TARGET_SEPARATOR = "___"


def _tool_from_context(context: Any) -> str:
    """The tool name the Gateway put in the client context, unqualified."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    qualified = str(custom.get(GATEWAY_TOOL_CONTEXT_KEY, ""))
    return qualified.rsplit(TARGET_SEPARATOR, 1)[-1] if qualified else ""


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


#: Keys the handler owns at the top level of an event. Anything else in a gateway invocation is
#: a tool argument, because a Gateway Lambda target receives the arguments as the event itself.
RESERVED_EVENT_KEYS = frozenset(
    {"tool", "operationId", "arguments", "claims", "tenant_id", "period"}
)

#: The period this deployment reports on. A gateway caller cannot supply it — `period` is in
#: `FORBIDDEN_ARGUMENTS` precisely because a scope must never be caller-supplied — so it comes
#: from the deployment, like the tenant does.
DEFAULT_PERIOD = os.environ.get("ATTESTOR_PERIOD", "2026")


def _arguments(event: dict[str, Any]) -> dict[str, Any]:
    """The tool's arguments, however this caller arrived.

    Our own tests and the runtime's HTTP surface nest them under `arguments`. AgentCore Gateway
    does not: a Lambda target is invoked with the tool input *as* the event, with the tool name
    off in the client context. So an event with no `arguments` key is one big argument object,
    minus the keys this handler owns.

    `FORBIDDEN_ARGUMENTS` still runs over the result, and matters more here than anywhere: on
    this path a smuggled `tenant_id` would arrive looking exactly like a legitimate argument.
    """
    if "arguments" in event:
        return dict(event["arguments"] or {})
    return {k: v for k, v in event.items() if k not in RESERVED_EVENT_KEYS}


#: The gateway that invoked this Lambda, as AgentCore names it: `attestor-gateway-<tenant>-<id>`.
GATEWAY_CONTEXT_KEY = "bedrockAgentCoreGatewayId"


def _declared_session(tenant_id: str, *, subject: str, period: str, session_id: str) -> Session:
    """A session for a surface that authenticates its caller and forwards nothing.

    Both AgentCore surfaces do this. The Gateway invokes a Lambda target under
    `GATEWAY_IAM_ROLE`, and its client context carries the tool name, the gateway id and the
    target id — no claims whatsoever. The Runtime authenticates a JWT at its own edge and the
    container behind it is not given the token either. In both cases the platform *is* the
    authorization boundary for that hop, and both hold: measured live, a helios token gets
    `insufficient_scope` at the aegis gateway and `request_override` is refused by Cedar before
    the Lambda is reached at all.

    So the two halves of a session come from the two strongest sources available.

    **The tenant is the surface.** One gateway per tenant, one runtime per tenant, and which one
    was called is asserted by AgentCore or set on the resource — never supplied by a caller.

    **The role is declared**, in `var.surface_roles`, and arrives as
    `ATTESTOR_SURFACE_ROLE_<TENANT>`. It cannot be derived, because the claims are not here, and
    this is the one place where a default would be easy and wrong: with no claims, "assume the
    least privilege" reads as prudence and is still a handler granting authority nobody wrote
    down. An unset or unknown role yields no session, and no session runs no tool.
    """
    registry = declarative().registry
    if tenant_id not in registry.ids:
        raise Rejected(f"{tenant_id!r} is not a tenant this deployment knows")

    role = os.environ.get(f"ATTESTOR_SURFACE_ROLE_{tenant_id.upper()}", "")
    if role not in ROLES:
        raise Rejected(
            f"the surface serving {tenant_id!r} carries no declared role. "
            f"ATTESTOR_SURFACE_ROLE_{tenant_id.upper()} is {role or 'unset'!r}; no claims reach "
            "this path, and a role is declared in `var.surface_roles` rather than assumed"
        )

    return Session(
        tenant=tenant_id,
        subject=subject,
        roles=frozenset({role}),
        period=period,
        session_id=session_id,
    )


def _gateway_session(context: Any, *, period: str, session_id: str) -> Session | None:
    """A session for a call that arrived through a gateway, or `None` if it did not."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    gateway = str(custom.get(GATEWAY_CONTEXT_KEY, ""))
    if not gateway:
        return None

    tenant_id = next(
        (t for t in declarative().registry.ids if gateway.startswith(f"{PROJECT}-gateway-{t}-")),
        "",
    )
    if not tenant_id:
        raise Rejected(f"gateway {gateway!r} names no tenant this deployment knows")
    return _declared_session(
        tenant_id, subject=f"gateway:{gateway}", period=period, session_id=session_id
    )


def _runtime_session(*, period: str, session_id: str) -> Session | None:
    """A session for a call that arrived at this tenant's runtime, or `None` off that path.

    `ATTESTOR_TENANT` is set on the runtime resource, one per tenant, the same way there is one
    gateway per tenant. A runtime has no client context to read an identity out of, so the fact
    is stated on the resource — where a caller cannot reach it — rather than inferred.
    """
    tenant_id = os.environ.get("ATTESTOR_TENANT", "")
    if not tenant_id:
        return None
    return _declared_session(
        tenant_id, subject=f"runtime:{tenant_id}", period=period, session_id=session_id
    )


def invoke(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Gateway → tool. Returns a JSON body; never raises past this boundary."""
    started = dt.datetime.now(dt.UTC)
    tool = str(event.get("tool") or event.get("operationId") or "") or _tool_from_context(context)
    arguments = _arguments(event)
    claims = dict(event.get("claims") or {})
    tenant_id = str(event.get("tenant_id", ""))
    period = str(event.get("period", "")) or DEFAULT_PERIOD
    # Not "local". A `Session` requires at least six characters, so the old default could not
    # construct one — and every invocation arriving without a Lambda context died as a 500
    # with a pydantic message, which reads as a broken tool rather than a missing correlation
    # id. The default is now valid *and* obviously a default.
    request_id = getattr(context, "aws_request_id", None) or "local-no-request-id"

    # The shape of what arrived, on every call. Keys only for the event and the context, plus
    # the values of AgentCore's own metadata — a claim value is identity and does not belong in
    # a log that operations reads.
    #
    # This is not scaffolding to remove. Three separate defects on this path were invisible
    # because nothing recorded what the caller actually sent: the tool name that was somewhere
    # else, the arguments that were somewhere else, and the claims. "unknown tool ''" told us a
    # tool was missing and nothing about where to look.
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    _emit(
        "invocation.shape",
        session_id=request_id,
        event_keys=sorted(event),
        context_keys=sorted(custom),
        agentcore={k: v for k, v in custom.items() if k.lower().startswith("bedrockagentcore")},
        resolved_tool=tool,
        resolved_tenant=tenant_id,
        claim_keys=sorted(claims),
    )

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

        # Two doors, two ways to be who you are. Through the gateway there are no claims to
        # map, so the session comes from what AgentCore asserts (the tenant) and what Terraform
        # declares (the role). Through the runtime's HTTP surface the claims are verified and
        # forwarded, and the registry's issuer and audience binding decides which undertaking
        # they speak for.
        # Claims first, when a caller actually presents them: the registry's issuer and
        # audience binding is the strongest statement available about which undertaking a
        # principal speaks for. Neither AgentCore surface forwards them, so both fall through
        # to what the platform asserts and what Terraform declares.
        if claims:
            session = build_session(
                claims, tenant_id=tenant_id, period=period, session_id=request_id
            )
        else:
            session = _gateway_session(
                context, period=period, session_id=request_id
            ) or _runtime_session(period=period, session_id=request_id)
        if session is None:
            raise Rejected(
                "no claims, no gateway and no runtime tenant; nothing here says who is calling"
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
