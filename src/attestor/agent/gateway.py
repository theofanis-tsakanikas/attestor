"""The OpenAPI description AgentCore Gateway turns into MCP tools.

Generated from `tools.SPECS`, never hand-written. A hand-maintained schema beside a handler
is two descriptions of one contract, and they diverge on the first busy afternoon — usually
in the direction of the schema promising a parameter the handler ignores.

The thing worth noticing is what the schema *cannot* express: there is no `tenant` property
on any operation, no `period`, no `principal`. Those arrive from the session, so the agent has
no vocabulary for asking about another undertaking. An injected instruction that says "fetch
this for tenant aegis" produces a schema validation error at the Gateway, not a decision
somewhere deeper that has to be right.
"""

from __future__ import annotations

import json
from typing import Any

from attestor.agent.tools import SPECS, ToolSpec

FORBIDDEN_PARAMETERS = frozenset({"tenant", "tenant_id", "period", "principal", "session", "role"})


def _operation(spec: ToolSpec) -> dict[str, Any]:
    properties = {
        name: {"type": "string", "description": description}
        for name, description in spec.parameters.items()
    }
    return {
        "operationId": spec.name,
        "summary": spec.summary,
        "requestBody": {
            "required": bool(spec.required),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": properties,
                        "required": list(spec.required),
                        # No extra properties: a model cannot smuggle a scope in beside a
                        # legitimate argument and hope something downstream reads it.
                        "additionalProperties": False,
                    }
                }
            },
        },
        "responses": {
            "200": {
                "description": "Result. Data only — no tool returns authority.",
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
            "403": {"description": "Refused by policy before the handler ran."},
        },
    }


def specification(*, title: str = "Attestor tools", version: str = "1.0.0") -> dict[str, Any]:
    paths = {f"/{spec.name}": {"post": _operation(spec)} for spec in SPECS}
    return {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": version,
            "description": (
                "Tools exposed to an agent through AgentCore Gateway. Tenant, period and "
                "principal are absent by design: they come from the authenticated session, "
                "so there is no vocabulary in which an agent can ask about another "
                "undertaking."
            ),
        },
        "paths": paths,
    }


def render(**kwargs: Any) -> str:
    return json.dumps(specification(**kwargs), indent=2, sort_keys=True) + "\n"


# ── The MCP tool schema AgentCore Gateway is configured with ─────────────────
#
# The OpenAPI document above describes the tools for a reader. This is the shape the Gateway
# target actually takes, and it is the one that decides whether the agent has any tools at
# all — a gateway with no target is a valid gateway serving an empty toolset, which fails
# silently and looks like a working deployment.
#
# Both come from `SPECS`, so the description a model reads, the schema the Gateway enforces
# and the signature the handler exposes are one statement rendered three ways.


def tool_schema() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": spec.name,
                "description": spec.summary,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        name: {"type": "string", "description": description}
                        for name, description in spec.parameters.items()
                    },
                    "required": list(spec.required),
                    # No `additionalProperties`. AgentCore's tool schema accepts exactly five
                    # keys — type, properties, required, items, description — and rejects the
                    # target outright otherwise: `Unknown parameter in
                    # toolSchema.inlinePayload[0].inputSchema: "additionalProperties"`.
                    #
                    # Nothing is lost, because nothing was enforcing it here anyway. A schema
                    # is enforced by whatever validates it, and the validator is
                    # `handler.invoke`, which compares the arguments against `spec.parameters`
                    # in code and refuses the extras itself. That is the control; this was a
                    # description of it. `scope_leaks()` covers the other half — no parameter
                    # exists that would let a caller name its own scope.
                },
            }
            for spec in SPECS
        ]
    }


def render_tool_schema() -> str:
    return json.dumps(tool_schema(), indent=2, sort_keys=True) + "\n"


def scope_leaks() -> tuple[str, ...]:
    """Any parameter that would let a caller name its own scope. Must always be empty."""
    return tuple(
        f"{spec.name}.{parameter}"
        for spec in SPECS
        for parameter in spec.parameters
        if parameter in FORBIDDEN_PARAMETERS
    )
