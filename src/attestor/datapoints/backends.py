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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

#: Reserved result columns. A query may return either alongside its figure; both are
#: optional, and a query that omits them behaves as before.
#:
#: `resolved_snapshot_id` is how a run pinned to "current" records what current *was* — the
#: single fact claim 4 needs and could not previously obtain from a live query.
#: `quarantined_rows` is how the lakehouse tells the resolver that the figure is computable
#: but not from clean data.
SNAPSHOT_COLUMN = "resolved_snapshot_id"
QUARANTINE_COLUMN = "quarantined_rows"


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
    #: What the query cost to run, in bytes. Feeds the per-tenant cost meter; not part of
    #: lineage, because a figure does not change because the scan got cheaper.
    scanned_bytes: int = 0


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
        timeout_seconds: float = 120.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._workgroup = workgroup
        self._catalog = catalog
        self._database = database
        self._output_location = output_location
        self._region = region
        self._client = client
        self._timeout_seconds = timeout_seconds
        # Injected so the polling loop is testable without a test that actually waits.
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    def _athena(self) -> Any:
        if self._client is None:
            import boto3  # noqa: PLC0415 — deliberately lazy; boto3 is an optional extra

            self._client = boto3.client("athena", region_name=self._region)
        return self._client

    def execute(
        self, *, sql: str, parameters: dict[str, str], snapshot_id: str | None
    ) -> QueryResult:
        """Run the statement with **bound** parameters and wait for one scalar.

        `ExecutionParameters` is positional in the Athena API, so named `:tenant_id` markers
        are rewritten to `?` in a fixed order and the values are passed alongside. The rewrite
        happens here and nowhere else — no caller ever builds a SQL string with a tenant id
        in it.
        """
        statement, ordered = _bind(sql, parameters, snapshot_id=snapshot_id)
        started = self._athena().start_query_execution(
            QueryString=statement,
            QueryExecutionContext={"Catalog": self._catalog, "Database": self._database},
            WorkGroup=self._workgroup,
            ResultConfiguration={"OutputLocation": self._output_location},
            ExecutionParameters=ordered,
        )
        execution_id = started["QueryExecutionId"]
        execution = self._await(execution_id)
        return self._scalar(execution_id, execution, sql=sql, snapshot_id=snapshot_id)

    def _await(self, execution_id: str) -> dict[str, Any]:
        """Poll until the query settles, or give up loudly.

        The timeout is not a nicety. A query that has not finished in this long has a missing
        predicate or a cold metastore, and a resolver that waits indefinitely turns one bad
        query into a report that never renders and never explains why.
        """
        deadline = self._clock() + self._timeout_seconds
        delay = 0.25
        while True:
            execution = self._athena().get_query_execution(QueryExecutionId=execution_id)[
                "QueryExecution"
            ]
            state = execution["Status"]["State"]
            if state == "SUCCEEDED":
                return execution
            if state in {"FAILED", "CANCELLED"}:
                reason = execution["Status"].get("StateChangeReason", state)
                raise QueryError(f"athena query {execution_id} {state.lower()}: {reason}")
            if self._clock() > deadline:
                self._athena().stop_query_execution(QueryExecutionId=execution_id)
                raise QueryError(
                    f"athena query {execution_id} exceeded {self._timeout_seconds}s and was "
                    "cancelled; a query this slow has a missing predicate"
                )
            self._sleep(delay)
            delay = min(delay * 2, 5.0)

    def _scalar(
        self,
        execution_id: str,
        execution: dict[str, Any],
        *,
        sql: str,
        snapshot_id: str | None,
    ) -> QueryResult:
        # Three, not two. The header occupies the first row, so `MaxResults=2` returns at most
        # one data row — which made the multi-row guard below unreachable and let a query
        # whose GROUP BY had escaped publish whichever group Athena happened to return first.
        # Asking for one more row than a scalar may have is what makes the guard able to fire.
        page = self._athena().get_query_results(QueryExecutionId=execution_id, MaxResults=3)
        rows = page.get("ResultSet", {}).get("Rows", [])
        header = [cell.get("VarCharValue") for cell in rows[0].get("Data", [])] if rows else []
        data = rows[1:]
        if len(data) > 1:
            raise QueryError(
                f"athena query {execution_id} returned {len(data)} rows where one was "
                "expected; a scalar resolver cannot choose between them"
            )
        columns = _columns(header, data[0]) if data else {}
        # The figure is the first column that is not one of the reserved diagnostics. Taking
        # `columns[0]` positionally would publish a snapshot id the day someone reorders a
        # SELECT list, and a reordered SELECT list is not supposed to be a restatement.
        raw = next(
            (v for k, v in columns.items() if k not in {SNAPSHOT_COLUMN, QUARANTINE_COLUMN}),
            None,
        )

        # Athena does not report which tables it read, so they are parsed from the statement
        # the resolver just sent. That is not a guess: the query text is the authority on what
        # it touched, and it is the same text whose digest goes into the lineage hash.
        tables = tables_in(sql)
        statistics = execution.get("Statistics", {})

        # A query may return its quarantine count and the snapshot it actually read alongside
        # the figure, in the reserved columns below. Both used to be hardcoded here — which
        # meant `E_UPSTREAM_QUARANTINE` could not occur against a real lakehouse however many
        # rows had failed their data contract, and a run pinned to "current" recorded no
        # snapshot at all, so claim 4 held offline and was unachievable live.
        resolved_snapshot = columns.get(SNAPSHOT_COLUMN) or snapshot_id
        quarantined = columns.get(QUARANTINE_COLUMN)
        return QueryResult(
            value=None if raw in (None, "") else Decimal(raw),
            tables=tables,
            snapshot_ids=(
                {table: str(resolved_snapshot) for table in tables} if resolved_snapshot else {}
            ),
            row_counts={},
            quarantined_rows=int(quarantined) if quarantined not in (None, "") else 0,
            scanned_bytes=int(statistics.get("DataScannedInBytes", 0)),
        )


#: `FROM`/`JOIN` followed by a qualified table name. Deliberately narrow: every query in
#: `queries/` is a reviewed statement in this shape, and a parser that accepted more would be
#: guessing about statements this repository does not contain.
_TABLE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)", re.IGNORECASE)

#: A bound parameter marker. Matched only where the surrounding text is code.
_MARKER = re.compile(r":([a-z_][a-z0-9_]*)")

#: Iceberg snapshot ids are integers. This is the only value in the system that reaches a SQL
#: statement by substitution rather than by binding — Athena will not take a table-version pin
#: as a parameter — so the shape is asserted here instead of trusted.
_SNAPSHOT_ID = re.compile(r"^[0-9]{1,20}$")


def _columns(header: list[str | None], row: dict[str, Any]) -> dict[str, str | None]:
    """Zip a result row against its header, tolerating a short row."""
    cells = [cell.get("VarCharValue") for cell in row.get("Data", [])]
    return {str(name): value for name, value in zip(header, cells, strict=False) if name}


def tables_in(sql: str) -> tuple[str, ...]:
    """Qualified table names a statement reads, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _TABLE.finditer(sql):
        seen.setdefault(match.group(1).lower(), None)
    return tuple(seen)


def _bind(
    sql: str, parameters: dict[str, str], *, snapshot_id: str | None = None
) -> tuple[str, list[str]]:
    """Rewrite `:name` markers to positional `?`, returning values in matching order.

    `:snapshot_id` is special: Athena will not accept a table-version pin as a bound
    parameter, so an unset snapshot is rewritten to `NULL` in the statement and the query's
    own `COALESCE` falls back to the current snapshot. What that resolved to is reported back
    in the query's `resolved_snapshot_id` column, so "current" is never what an auditor has
    to re-read.

    Because it is substituted rather than bound, its shape is checked first. Every other
    value in this system reaches SQL as a parameter; this one cannot, so it earns the one
    validation the others do not need.
    """
    if snapshot_id is not None and not _SNAPSHOT_ID.match(str(snapshot_id)):
        raise QueryError(
            f"snapshot id {snapshot_id!r} is not an Iceberg snapshot id. It is the one "
            "value substituted into a statement rather than bound to it, so it is "
            "refused rather than escaped."
        )

    ordered: list[str] = []

    def replace(name: str) -> str:
        if name == "snapshot_id":
            return "NULL" if snapshot_id is None else f"'{snapshot_id}'"
        if name not in parameters:
            raise QueryError(f"query binds :{name} but no value was supplied")
        ordered.append(str(parameters[name]))
        return "?"

    statement = _substitute_outside_literals(sql, replace)
    return statement, ordered


def _substitute_outside_literals(sql: str, replace: Callable[[str], str]) -> str:
    """Rewrite `:name` markers, but only where they are code.

    Every query in `queries/` documents its own parameters in a comment header —
    `-- :tenant_id · :period_start · :period_end`. Substituting there appended a value for a
    marker Athena never saw, because Athena strips comments before counting placeholders. The
    result was `INVALID_PARAMETER_USAGE: Incorrect number of parameters: expected 6 but found
    9`, on every quantitative datapoint, on the live path only — the recorded backend does no
    binding at all, so nothing offline could have caught it.

    String literals are skipped for the older reason: a marker inside quotes is text, and
    turning it into a placeholder changes what the query says.
    """
    out: list[str] = []
    index, length = 0, len(sql)
    while index < length:
        character = sql[index]
        if character == "'":
            end = index + 1
            while end < length:
                if sql[end] == "'":
                    if end + 1 < length and sql[end + 1] == "'":
                        end += 2
                        continue
                    break
                end += 1
            out.append(sql[index : end + 1])
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            out.append(sql[index:end])
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(sql[index:end])
            index = end
        else:
            match = _MARKER.match(sql, index)
            if match:
                out.append(replace(match.group(1)))
                index = match.end()
            else:
                out.append(character)
                index += 1
    return "".join(out)
