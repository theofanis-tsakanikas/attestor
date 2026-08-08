"""AgentCore Memory: what an analyst asked this tenant's system, and what it answered.

An analyst's questions arrive one at a time and mean nothing one at a time. *Why is Scope 1
not disclosed?* is followed by *and what would unblock it?*, and the second question is only
answerable if something remembers the first. That continuity is what this is for, and it is
the reason the memory resource is in the estate rather than a box on an architecture diagram.

Three rules, and they are the same three the tools obey.

**The namespace comes from the session, never from an argument.** `attestor/<tenant>/<session>`
is derived here from a `Session` that was built from verified claims. There is no parameter a
caller could set to write into, or read from, another undertaking's memory — probe 9 of the
isolation suite is about exactly this, and it now has a live surface to be about.

**Nothing here decides anything.** It records what happened after it happened. No tool reads
memory to authorize, to resolve or to abstain; a figure is never recalled, it is re-derived.
If this module returned wrong answers, or no answers, every number in every report would be
identical — which is the property that makes it safe to let it fail.

**It fails open, loudly, and quickly.** A memory write is operational continuity, not safety or
compliance, and the doctrine splits on exactly that line: fail closed on a guardrail, fail open
on a reranker. An analyst's question is answered whether or not the recording of it succeeded,
and the failure is logged rather than raised.

The third word is the one that was missing. Fail-open catches an *error*, and a call with
nowhere to go does not produce one — it hangs. This module was deployed into a VPC whose egress
is the VPC and nothing else, before `bedrock-agentcore` had an endpoint there, and the tool
handler resolved its answer in seconds and then sat inside `create_event` until Lambda killed
it at 180 seconds. The caller was told "An internal error occurred" about work that had already
succeeded. A degradation with no deadline is not a degradation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

from attestor.policy.tenants import Session

LOGGER = logging.getLogger("attestor.agent.memory")

#: How much of a tool's result is worth remembering. A conversation needs the shape of the
#: answer, not the answer — the answer is re-derived from contracts and the lakehouse every
#: time, which is the only reason a memory of it is harmless.
SUMMARY_LIMIT = 600


class MemoryUnavailable(RuntimeError):
    """No memory is configured for this tenant. Recorded, never raised at a caller."""


def memory_id(tenant: str) -> str:
    """The memory this tenant's events belong in.

    `ATTESTOR_MEMORY_<TENANT>`, set by Terraform from the memory it created for that tenant.
    One memory per tenant rather than one shared memory with a namespace column: a filter is
    something that can be forgotten on one query, and a separate resource cannot be.
    """
    value = os.environ.get(f"ATTESTOR_MEMORY_{tenant.upper()}", "")
    if not value:
        raise MemoryUnavailable(f"ATTESTOR_MEMORY_{tenant.upper()} is unset")
    return value


def namespace(session: Session) -> str:
    """Derived, never supplied. The tenant here came from a verified token."""
    return f"attestor/{session.tenant}/{session.session_id}"


def record_invocation(
    session: Session,
    *,
    tool: str,
    outcome: str,
    detail: str = "",
    client: Any = None,
    now: dt.datetime | None = None,
) -> bool:
    """Remember that this session called this tool and what came back.

    Returns whether the event was written, so a caller that cares can say so. Nothing in the
    tool path cares: the answer is already computed by the time this runs.
    """
    try:
        target = memory_id(session.tenant)
    except MemoryUnavailable as exc:
        LOGGER.warning("memory not recorded: %s", exc)
        return False

    payload = json.dumps(
        {
            "tool": tool,
            "outcome": outcome,
            "detail": detail[:SUMMARY_LIMIT],
            "tenant": session.tenant,
            "period": session.period,
            "subject": session.subject,
        },
        sort_keys=True,
    )

    try:
        agentcore = client or _client()
        agentcore.create_event(
            memoryId=target,
            actorId=session.subject,
            sessionId=session.session_id,
            eventTimestamp=now or dt.datetime.now(tz=dt.UTC),
            payload=[{"conversational": {"role": "ASSISTANT", "content": {"text": payload}}}],
        )
    except Exception as exc:
        # Deliberately broad. Everything from a missing service model to a throttle to an
        # expired credential lands here, and every one of them has the same correct response:
        # the analyst still gets their answer, and someone gets told the memory is not being
        # written. Narrowing this would turn a logged degradation into a 500 on a read.
        LOGGER.warning("memory not recorded for %s/%s: %s", session.tenant, tool, exc)
        return False
    return True


def recent(session: Session, *, limit: int = 10, client: Any = None) -> list[dict[str, Any]]:
    """This session's own history, most recent first, scoped by construction.

    `memoryId` is this tenant's and `sessionId` is this session's; both come from the session
    object. There is no argument shaped like "whose".
    """
    try:
        target = memory_id(session.tenant)
        agentcore = client or _client()
        response = agentcore.list_events(
            memoryId=target,
            actorId=session.subject,
            sessionId=session.session_id,
            maxResults=limit,
        )
    except Exception as exc:
        LOGGER.warning("memory not read for %s: %s", session.tenant, exc)
        return []
    return list(response.get("events", []))


#: How long a memory write may take before it is abandoned. Seconds, not minutes: nothing waits
#: on this, and the answer it accompanies is already computed. The numbers are deliberately
#: smaller than any caller's patience — see the module docstring for what an unbounded one cost.
CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 3
ATTEMPTS = 1


def client_settings() -> dict[str, Any]:
    """The deadline this client is built with, as data.

    Separated from the client itself so the suite can assert on it without importing botocore.
    Offline is the default here: the whole test run has to work on a laptop with no AWS
    dependencies installed, and a test that reaches for `botocore.config` to check a number is a
    test that only runs where somebody happened to have it. CI caught exactly that.
    """
    return {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "read_timeout": READ_TIMEOUT_SECONDS,
        "retries": {"max_attempts": ATTEMPTS},
    }


def _client() -> Any:
    import boto3  # noqa: PLC0415 — optional dependency, never imported offline
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "bedrock-agentcore",
        region_name=os.environ.get("AWS_REGION", "eu-central-1"),
        config=Config(**client_settings()),
    )
