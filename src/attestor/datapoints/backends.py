"""Where a figure actually comes from.

Two implementations, and the split matters more than either of them.

`RecordedBackend` replays results captured from a real run. Every test, every eval and every
gate in this repository uses it, which is why the whole suite runs on a laptop with no AWS
account. A recording carries the query digest it was captured against, so editing a query
without re-capturing fails loudly rather than silently replaying the old answer.

`AthenaBackend` talks to the real lakehouse. It is imported lazily and never at module
import time, so `boto3` stays an optional dependency and no offline run can accidentally
reach for a network.

Both are the same protocol, and the resolver cannot tell them apart. That is the point: the
logic under test is the logic that runs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml


class QueryError(RuntimeError):
    """The query did not produce a usable answer. Always surfaces as E_RESOLVER_ERROR."""


class StaleRecording(QueryError):
    """A recorded result was captured against a different version of the query.

    Deliberately fatal. A replay that silently answers with the previous query's result is
    a test suite that passes while the thing it tests has changed.
    """


@dataclass(frozen=True, slots=True)
class QueryResult:
    """A scalar answer, plus what it was read from."""

    #: `None` means the query ran and matched nothing. That is not zero, and the resolver
    #: treats it as an evidence problem rather than a value.
    value: Decimal | None
    tables: tuple[str, ...] = ()
    snapshot_ids: dict[str, str] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)
    #: Rows that failed their data contract and were excluded. Non-zero means the figure is
    #: computed over incomplete data, which the resolver turns into E_UPSTREAM_QUARANTINE.
    quarantined_rows: int = 0


def query_digest(sql: str) -> str:
    """Hash of the query text, whitespace-normalised so reformatting is not a restatement."""
    normalised = " ".join(sql.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@runtime_checkable
class QueryBackend(Protocol):
    def execute(
        self, *, sql: str, parameters: dict[str, str], snapshot_id: str | None
    ) -> QueryResult: ...


class RecordedBackend:
    """Replays captured results. The offline default for tests, evals and gates."""

    def __init__(self, recordings: dict[str, dict[str, Any]]) -> None:
        self._recordings = recordings

    @classmethod
    def from_directory(cls, directory: Path | str) -> RecordedBackend:
        """Load `*.yaml` recordings keyed by `query_digest + parameters`."""
        directory = Path(directory)
        recordings: dict[str, dict[str, Any]] = {}
        if not directory.is_dir():
            return cls(recordings)
        for path in sorted(directory.rglob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for entry in payload.get("results", []):
                recordings[cls._key(entry["query_digest"], entry.get("parameters", {}))] = entry
        return cls(recordings)

    @staticmethod
    def _key(digest: str, parameters: dict[str, Any]) -> str:
        bound = ";".join(f"{k}={v}" for k, v in sorted(parameters.items()))
        return f"{digest}|{bound}"

    def execute(
        self, *, sql: str, parameters: dict[str, str], snapshot_id: str | None
    ) -> QueryResult:
        digest = query_digest(sql)
        entry = self._recordings.get(self._key(digest, parameters))
        if entry is None:
            # Distinguish "never recorded" from "recorded against a different query text".
            # The second is the dangerous one and deserves its own error.
            for recorded in self._recordings.values():
                if recorded.get("parameters", {}) == parameters:
                    raise StaleRecording(
                        f"a recording exists for these parameters but was captured against "
                        f"query {recorded['query_digest'][:12]}, and the query now digests to "
                        f"{digest[:12]}. Re-capture it rather than replaying a stale answer."
                    )
            raise QueryError(
                f"no recorded result for query {digest[:12]} with parameters {parameters}"
            )
        raw = entry.get("value")
        return QueryResult(
            value=None if raw is None else Decimal(str(raw)),
            tables=tuple(entry.get("tables", ())),
            snapshot_ids=dict(entry.get("snapshot_ids", {})),
            row_counts=dict(entry.get("row_counts", {})),
            quarantined_rows=int(entry.get("quarantined_rows", 0)),
        )


class AthenaBackend:
    """The real lakehouse. Imported lazily; never reached by an offline run."""

    def __init__(
        self,
        *,
        workgroup: str,
        catalog: str,
        database: str,
        output_location: str,
        region: str,
        client: Any = None,
    ) -> None:
        self._workgroup = workgroup
        self._catalog = catalog
        self._database = database
        self._output_location = output_location
        self._region = region
        self._client = client

    def _athena(self) -> Any:
        if self._client is None:
            import boto3  # noqa: PLC0415 — deliberately lazy; boto3 is an optional extra

            self._client = boto3.client("athena", region_name=self._region)
        return self._client

    def execute(
        self, *, sql: str, parameters: dict[str, str], snapshot_id: str | None
    ) -> QueryResult:
        """Run the statement with **bound** parameters.

        `ExecutionParameters` is positional in the Athena API, so named `:tenant_id` markers
        are rewritten to `?` in a fixed order and the values are passed alongside. The
        rewrite happens here and nowhere else — no caller ever builds a SQL string with a
        tenant id in it.
        """
        statement, ordered = _bind(sql, parameters)
        response = self._athena().start_query_execution(
            QueryString=statement,
            QueryExecutionContext={"Catalog": self._catalog, "Database": self._database},
            WorkGroup=self._workgroup,
            ResultConfiguration={"OutputLocation": self._output_location},
            ExecutionParameters=ordered,
        )
        raise QueryError(
            "AthenaBackend.execute is wired for submission only until the estate is stood up; "
            f"query {response.get('QueryExecutionId', '?')} was submitted but result polling "
            "is deliberately unimplemented offline"
        )


def _bind(sql: str, parameters: dict[str, str]) -> tuple[str, list[str]]:
    """Rewrite `:name` markers to positional `?`, returning values in matching order."""
    import re  # noqa: PLC0415 — local to keep the module's import surface honest

    ordered: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            raise QueryError(f"query binds :{name} but no value was supplied")
        ordered.append(str(parameters[name]))
        return "?"

    statement = re.sub(r":([a-z_][a-z0-9_]*)", replace, sql)
    return statement, ordered
