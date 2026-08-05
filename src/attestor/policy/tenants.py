"""The tenant registry, and the session that carries a tenant's identity.

Every scoped thing in this system — the evidence index, the retrieval filter, the query
parameters, the Cedar request — takes its tenant from a `Session`, and a `Session` is built
once, from the identity provider's claims, at the start of a request.

The rule that follows from that is short and is the whole of claim 2's structural half:

    **There is no ambient tenant.**

No module reads a tenant from configuration, from an environment variable, from a default
argument or from anything a model said. If a function needs to know which undertaking it is
working for, it takes a `Session`, and the `Session` came from a token.

Roles arrive the same way. A principal's groups are resolved from IdP claims at session
creation, so a principal cannot talk its way into `role:preparer` during a conversation —
by the time the conversation exists, the membership is already fixed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from attestor.policy.cedar import Entity, Principal, Request, Resource

TENANTS_DIR = "tenants"

#: Roles a claim may map to. Closed, because an unrecognised role in a token must not become
#: an unrecognised group that silently matches no policy and fails open somewhere else.
ROLES = frozenset({"role:reporter", "role:preparer", "role:auditor"})


class IdentityProvider(BaseModel):
    """How this tenant's people authenticate.

    Three tenants, deliberately not all on the same provider. If every tenant used Cognito,
    "the identity layer is per-tenant" would be a claim about configuration rather than about
    the design, and the first real customer with Entra ID would find out which.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cognito", "oidc"]
    issuer: str = Field(min_length=8)
    audience: str = Field(min_length=3)
    #: The claim carrying group membership. Providers disagree; the mapping is per tenant.
    groups_claim: str = "cognito:groups"
    #: Maps a provider's group name onto one of `ROLES`.
    role_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _roles_are_known(self) -> Self:
        unknown = sorted(set(self.role_map.values()) - ROLES)
        if unknown:
            raise ValueError(
                f"unknown role(s) {', '.join(unknown)}; permitted: {', '.join(sorted(ROLES))}"
            )
        return self


class Tenant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=3)
    standard: Literal["ESRS", "EU_AI_ACT"]

    #: The vector-store namespace and the S3 prefix. Derived from the id rather than
    #: configured, so a copy-paste cannot point two tenants at one namespace.
    @property
    def namespace(self) -> str:
        return f"attestor/{self.id}"

    identity: IdentityProvider
    periods: tuple[str, ...] = ()
    #: Contact for the omissions register. An override names people; this names the desk.
    reporting_contact: str = ""

    @classmethod
    def load_all(cls, root: Path | str = ".") -> tuple[Tenant, ...]:
        directory = Path(root) / TENANTS_DIR
        tenants = tuple(
            cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
            for path in sorted(directory.glob("*.yaml"))
        )
        ids = [tenant.id for tenant in tenants]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate tenant id in the registry")
        return tenants


class TenantRegistry:
    def __init__(self, tenants: tuple[Tenant, ...]) -> None:
        self._tenants = {tenant.id: tenant for tenant in tenants}

    @classmethod
    def load(cls, root: Path | str = ".") -> TenantRegistry:
        return cls(Tenant.load_all(root))

    def __iter__(self) -> Iterator[Tenant]:
        return iter(self._tenants.values())

    def __len__(self) -> int:
        return len(self._tenants)

    def __getitem__(self, tenant_id: str) -> Tenant:
        try:
            return self._tenants[tenant_id]
        except KeyError:
            raise KeyError(f"no tenant {tenant_id!r} in the registry") from None

    def get(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tenants))


class UnknownRole(ValueError):
    """A token carried a group that maps to nothing. The session is not created."""


class Session(BaseModel):
    """One authenticated principal, scoped to one tenant, for the life of one request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: str
    subject: str = Field(min_length=3)
    roles: frozenset[str]
    period: str = Field(pattern=r"^\d{4}(-Q[1-4])?$")
    #: Opaque id for correlating traces and the cost meter.
    session_id: str = Field(min_length=6)

    @model_validator(mode="after")
    def _roles_are_known(self) -> Self:
        unknown = sorted(self.roles - ROLES)
        if unknown:
            raise UnknownRole(f"session carries unknown role(s): {', '.join(unknown)}")
        if not self.roles:
            raise ValueError("a session with no role can do nothing and should not exist")
        return self

    @classmethod
    def from_claims(
        cls,
        claims: dict[str, object],
        *,
        tenant: Tenant,
        period: str,
        session_id: str,
    ) -> Session:
        """Build a session from a verified token's claims.

        The token is assumed already verified — signature, issuer, audience and expiry are
        the gateway's job. What happens here is the mapping from *this provider's* group
        names to our roles, which is per tenant precisely because providers disagree.

        A group the tenant's map does not know is dropped rather than passed through. Passing
        it through would put an unrecognised string into a policy evaluation, where it would
        match no permit and produce a deny that looks like a policy decision instead of a
        configuration error.
        """
        raw = claims.get(tenant.identity.groups_claim, [])
        groups = raw if isinstance(raw, list) else [raw]
        roles = {
            tenant.identity.role_map[str(group)]
            for group in groups
            if str(group) in tenant.identity.role_map
        }
        if not roles:
            raise UnknownRole(
                f"no group in {sorted(str(g) for g in groups)} maps to a role for "
                f"tenant {tenant.id}"
            )
        return cls(
            tenant=tenant.id,
            subject=str(claims.get("sub", "")),
            roles=frozenset(roles),
            period=period,
            session_id=session_id,
        )

    # ── Deriving everything else from the session ────────────────────────────

    def principal(self) -> Principal:
        return Principal(
            entity=Entity("User", f"{self.tenant}:{self.subject}"),
            attributes={"tenant": self.tenant},
            parents=frozenset(Entity("Group", role) for role in sorted(self.roles)),
        )

    def resource(self, kind: str, identifier: str, *, tenant: str | None = None) -> Resource:
        """A resource in *this* session's tenant unless a caller explicitly names another.

        The override exists so the isolation suite can construct the attack it is testing.
        Nothing in production passes it, and `evals/isolation/` is where that is checked.
        """
        owner = tenant or self.tenant
        return Resource(
            entity=Entity(kind, f"{owner}:{identifier}"),
            attributes={"tenant": owner},
            parents=frozenset({Entity("Namespace", owner)}),
        )

    def request(
        self,
        action: str,
        resource: Resource,
        *,
        filter_tenant: str | None = None,
        session_tenant: str | None = None,
    ) -> Request:
        return Request(
            principal=self.principal(),
            action=action,
            resource=resource,
            context={
                "session_tenant": session_tenant or self.tenant,
                "filter_tenant": filter_tenant or self.tenant,
                "session_id": self.session_id,
                "period": self.period,
            },
        )

    def retrieval_filter(self) -> dict[str, str]:
        """The metadata filter every retrieval carries. Built here, never from a prompt."""
        return {"tenant": self.tenant, "period": self.period}
