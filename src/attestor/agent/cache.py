"""A cache whose key cannot omit the tenant.

Caching a retrieval or a resolved figure is the obvious optimisation and the classic
multi-tenant leak: two tenants ask the same question, the second gets the first one's answer,
and nothing anywhere logs an error because from the cache's point of view it worked.

The defence is not "remember to include the tenant". It is that there is no way to build a
key without one. `CacheKey` takes the tenant as a required field, hashes it into the digest,
and `TenantCache` refuses a lookup whose key names a different tenant than the session it was
handed — so even a key constructed correctly cannot be *used* across the boundary.

The other half is what is deliberately absent: there is no `clear()`, no global namespace and
no shared instance. A cache is created per tenant and dies with the request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class CacheScopeError(RuntimeError):
    """A key from one tenant was used against another tenant's cache."""


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Everything that makes two questions the same question."""

    tenant: str
    period: str
    kind: str
    #: The rest of the question: a query digest, a datapoint id, a filter.
    parts: tuple[tuple[str, str], ...] = ()

    @classmethod
    def of(cls, *, tenant: str, period: str, kind: str, **parts: Any) -> CacheKey:
        return cls(
            tenant=tenant,
            period=period,
            kind=kind,
            parts=tuple(sorted((str(k), str(v)) for k, v in parts.items())),
        )

    @property
    def digest(self) -> str:
        payload = {
            # First, and not optional. The whole module exists for this line.
            "tenant": self.tenant,
            "period": self.period,
            "kind": self.kind,
            "parts": [list(part) for part in self.parts],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def __str__(self) -> str:
        return f"{self.kind}:{self.tenant}:{self.digest[:12]}"


class TenantCache:
    """An in-memory cache bound to exactly one tenant."""

    def __init__(self, tenant: str) -> None:
        self._tenant = tenant
        self._entries: dict[str, Any] = {}
        self.hits = 0
        self.misses = 0

    @property
    def tenant(self) -> str:
        return self._tenant

    def _check(self, key: CacheKey) -> None:
        if key.tenant != self._tenant:
            raise CacheScopeError(
                f"key belongs to tenant {key.tenant!r} and this cache serves {self._tenant!r}; "
                "a cross-tenant cache read is refused rather than served"
            )

    def get(self, key: CacheKey) -> Any | None:
        self._check(key)
        value = self._entries.get(key.digest)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, key: CacheKey, value: Any) -> Any:
        self._check(key)
        self._entries[key.digest] = value
        return value

    def __len__(self) -> int:
        return len(self._entries)
