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

from attestor.agent.tools import SPECS, Denied, Toolbox
from attestor.contracts import loader, overrides
from attestor.contracts.model import Standard
from attestor.datapoints.backends import AthenaBackend
from attestor.datapoints.evidence import EvidenceIndex
from attestor.datapoints.resolver import Resolver
from attestor.policy import cedar
from attestor.policy.tenants import Session, TenantRegistry, UnknownRole

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


def build_session(claims: dict[str, Any], *, tenant_id: str, period: str, session_id: str):
    registry = TenantRegistry.load(ROOT)
    tenant = registry.get(tenant_id)
    if tenant is None:
        raise Rejected(f"unknown tenant {tenant_id!r}")
    return Session.from_claims(claims, tenant=tenant, period=period, session_id=session_id)


def build_toolbox(session: Session) -> Toolbox:
    """Wire a toolbox for one session. Every collaborator is scoped by it."""
    contracts = loader.load(ROOT)
    registry = TenantRegistry.load(ROOT)
    standard = Standard(registry[session.tenant].standard)

    backend = AthenaBackend(
        workgroup=os.environ["ATTESTOR_WORKGROUP"],
        catalog=os.environ.get("ATTESTOR_CATALOG", "AwsDataCatalog"),
        database=os.environ.get("ATTESTOR_DATABASE", "attestor_gold"),
        output_location=os.environ["ATTESTOR_ATHENA_OUTPUT"],
        region=os.environ.get("AWS_REGION", "eu-central-1"),
    )
    resolver = Resolver(
        contracts=contracts.for_standard(standard),
        backend=backend,
        evidence=EvidenceIndex.for_tenant(ROOT, session.tenant),
        override_register=overrides.load_register(ROOT),
        root=ROOT,
    )
    return Toolbox(
        session=session,
        policies=cedar.load(ROOT),
        contracts=contracts.for_standard(standard),
        resolver=resolver,
        overrides=overrides.load_register(ROOT),
        retrieval=_retrieval(),
    )


def _retrieval():
    from attestor.agent.bedrock import BedrockRetrieval  # noqa: PLC0415 — needs boto3

    return BedrockRetrieval(
        region=os.environ.get("AWS_REGION", "eu-central-1"),
        evidence_kb_id=os.environ["ATTESTOR_EVIDENCE_KB"],
        regulatory_kb_id=os.environ["ATTESTOR_REGULATORY_KB"],
    )


def invoke(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Gateway → tool. Returns a JSON body; never raises past this boundary."""
    started = dt.datetime.now(dt.UTC)
    tool = str(event.get("tool") or event.get("operationId") or "")
    arguments = dict(event.get("arguments") or {})
    claims = dict(event.get("claims") or {})
    request_id = getattr(context, "aws_request_id", "local")

    try:
        smuggled = sorted(set(arguments) & FORBIDDEN_ARGUMENTS)
        if smuggled:
            # The schema has no such properties, so these did not come from a well-formed
            # call. Refusing is what makes the attempt appear in CloudWatch.
            raise Rejected(
                f"arguments {', '.join(smuggled)} name a scope; scope comes from the session"
            )

        session = build_session(
            claims,
            tenant_id=str(event.get("tenant_id", "")),
            period=str(event.get("period", "")),
            session_id=request_id,
        )
        toolbox = build_toolbox(session)

        handler = getattr(toolbox, tool, None)
        if handler is None or tool not in {spec.name for spec in SPECS}:
            raise Rejected(f"unknown tool {tool!r}")

        result = handler(**arguments)
        _emit(
            "tool.invoked",
            tool=tool,
            tenant=session.tenant,
            session_id=session.session_id,
            ms=_elapsed(started),
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
        return {"statusCode": 403, "body": {"error": str(denied)}}

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
