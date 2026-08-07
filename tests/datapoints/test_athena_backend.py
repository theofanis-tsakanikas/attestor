"""The Athena edge, driven through its real code path with a stub client.

Everything here was previously either unreachable or hardcoded, which is a specific kind of
dangerous: the offline suite exercised branches the live backend could not take, so a control
could be green in replay and absent in production.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest

from attestor.datapoints.backends import AthenaBackend, QueryError, _bind, tables_in


class StubAthena:
    """Just enough Athena to drive `execute` end to end."""

    def __init__(self, rows: list[list[str]], *, scanned: int = 4096) -> None:
        self.rows = rows
        self.scanned = scanned
        self.started: dict[str, Any] = {}
        self.max_results: int | None = None

    def start_query_execution(self, **kwargs: Any) -> dict[str, Any]:
        self.started = kwargs
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "QueryExecution": {
                "Status": {"State": "SUCCEEDED"},
                "Statistics": {"DataScannedInBytes": self.scanned},
            }
        }

    def get_query_results(self, **kwargs: Any) -> dict[str, Any]:
        self.max_results = kwargs.get("MaxResults")
        limited = self.rows[: self.max_results or len(self.rows)]
        return {
            "ResultSet": {"Rows": [{"Data": [{"VarCharValue": c} for c in row]} for row in limited]}
        }


def _backend(client: StubAthena) -> AthenaBackend:
    return AthenaBackend(
        workgroup="attestor",
        catalog="AwsDataCatalog",
        database="attestor_gold",
        output_location="s3://bucket/results/",
        region="eu-central-1",
        client=client,
        clock=lambda: 0.0,
        sleep=lambda _s: None,
    )


PARAMS = {"tenant_id": "helios", "period_start": "2026-01-01", "period_end": "2027-01-01"}
SQL = "SELECT v AS value FROM gold.t WHERE t.tenant_id = :tenant_id"


# ── The multi-row guard has to be able to fire ───────────────────────────────


def test_a_scalar_query_returning_two_rows_is_refused() -> None:
    """A GROUP BY that escaped must not publish whichever group came back first.

    `MaxResults=2` returns the header plus at most one data row, so the guard below could
    never see a second row and the check was decorative. The backend now asks for three.
    """
    client = StubAthena([["value"], ["10"], ["20"]])
    with pytest.raises(QueryError, match="where one was expected"):
        _backend(client).execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
    assert client.max_results == 3


def test_one_row_is_read_normally() -> None:
    client = StubAthena([["value"], ["18422.4118"]])
    result = _backend(client).execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
    assert result.value == Decimal("18422.4118")
    assert result.scanned_bytes == 4096


def test_no_rows_is_none_not_zero() -> None:
    """An empty result is an evidence problem, and the resolver depends on the distinction."""
    assert (
        _backend(StubAthena([["value"]]))
        .execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
        .value
        is None
    )


# ── Claim 4 on the live path ─────────────────────────────────────────────────


def test_the_snapshot_a_run_actually_read_is_reported() -> None:
    """ "Current" is not something an auditor can re-read a year later.

    The backend used to record a snapshot only when the caller had already supplied one —
    that is, only when reproducibility was not the question. A run pinned to current recorded
    nothing, so re-resolving as of that instant was impossible.
    """
    client = StubAthena([["value", "resolved_snapshot_id"], ["12.5", "7284419023871123001"]])
    result = _backend(client).execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
    assert result.value == Decimal("12.5")
    assert result.snapshot_ids == {"gold.t": "7284419023871123001"}


def test_the_figure_is_the_first_non_reserved_column() -> None:
    """Reordering a SELECT list must not publish a snapshot id as a disclosure."""
    client = StubAthena([["resolved_snapshot_id", "value"], ["7284419023871123001", "12.5"]])
    result = _backend(client).execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
    assert result.value == Decimal("12.5")


# ── Quarantine on the live path ──────────────────────────────────────────────


def test_quarantined_rows_are_reported() -> None:
    """Otherwise E_UPSTREAM_QUARANTINE can occur in replay and never in production."""
    client = StubAthena([["value", "quarantined_rows"], ["51204.0", "1284"]])
    result = _backend(client).execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
    assert result.quarantined_rows == 1284


def test_a_query_without_the_reserved_columns_still_works() -> None:
    """Both columns are optional; adding them was not allowed to be a breaking change."""
    result = _backend(StubAthena([["value"], ["7"]])).execute(
        sql=SQL, parameters=PARAMS, snapshot_id=None
    )
    assert result.quarantined_rows == 0
    assert result.snapshot_ids == {}


# ── The one value that is substituted rather than bound ──────────────────────


@pytest.mark.parametrize(
    "hostile",
    ["1' OR '1'='1", "abc", "1); DROP TABLE gold.t; --", "1 UNION SELECT 1", ""],
)
def test_a_snapshot_id_that_is_not_a_snapshot_id_is_refused(hostile: str) -> None:
    """Athena will not bind a table-version pin, so this is the one substitution point."""
    with pytest.raises(QueryError, match="not an Iceberg snapshot id"):
        _bind(SQL + " FOR VERSION AS OF :snapshot_id", PARAMS, snapshot_id=hostile)


def test_a_real_snapshot_id_is_accepted() -> None:
    statement, _ = _bind(
        "SELECT 1 FROM gold.t FOR VERSION AS OF :snapshot_id", {}, snapshot_id="7284419023871123001"
    )
    assert "'7284419023871123001'" in statement


def test_an_unset_snapshot_becomes_null() -> None:
    statement, _ = _bind("SELECT 1 FROM gold.t FOR VERSION AS OF :snapshot_id", {})
    assert "NULL" in statement


def test_the_tenant_is_bound_never_concatenated() -> None:
    statement, ordered = _bind(SQL, PARAMS)
    assert "helios" not in statement
    assert "?" in statement
    assert ordered == ["helios"]


def test_a_bound_marker_with_no_value_is_refused() -> None:
    with pytest.raises(QueryError, match="no value was supplied"):
        _bind("SELECT 1 FROM gold.t WHERE a = :nobody_supplied_this", {})


# ── Failure modes ────────────────────────────────────────────────────────────


def test_a_failed_query_surfaces_its_reason() -> None:
    class Failing(StubAthena):
        def get_query_execution(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "QueryExecution": {
                    "Status": {"State": "FAILED", "StateChangeReason": "SYNTAX_ERROR"}
                }
            }

    with pytest.raises(QueryError, match="SYNTAX_ERROR"):
        _backend(Failing([])).execute(sql=SQL, parameters=PARAMS, snapshot_id=None)


def test_a_query_that_never_settles_is_cancelled() -> None:
    """A resolver that waits forever turns one bad query into a report that never explains."""
    ticks = iter([0.0, 0.0, 1e9, 1e9, 1e9])
    cancelled: list[str] = []

    class Pending(StubAthena):
        def get_query_execution(self, **_kwargs: Any) -> dict[str, Any]:
            return {"QueryExecution": {"Status": {"State": "RUNNING"}}}

        def stop_query_execution(self, **kwargs: Any) -> dict[str, Any]:
            cancelled.append(kwargs["QueryExecutionId"])
            return {}

    backend = AthenaBackend(
        workgroup="attestor",
        catalog="AwsDataCatalog",
        database="attestor_gold",
        output_location="s3://bucket/results/",
        region="eu-central-1",
        client=Pending([]),
        timeout_seconds=1.0,
        clock=lambda: next(ticks),
        sleep=lambda _s: None,
    )
    with pytest.raises(QueryError, match="missing predicate"):
        backend.execute(sql=SQL, parameters=PARAMS, snapshot_id=None)
    assert cancelled == ["q-1"]


# ── Table extraction ─────────────────────────────────────────────────────────


def test_the_snapshots_metadata_table_is_not_recorded_as_a_source() -> None:
    """It is where the snapshot id is read from, not a table the figure came from."""
    sql = (
        'SELECT v AS value, (SELECT MAX(s.snapshot_id) FROM "gold"."t$snapshots" AS s) '
        "AS resolved_snapshot_id FROM gold.t"
    )
    assert tables_in(sql) == ("gold.t",)


class TestParameterMarkersInComments:
    """The binding must not treat documentation as code.

    Every file in `queries/` opens with a header naming its parameters —
    `-- :tenant_id · :period_start · :period_end`. Substituting there appends a value for a
    marker Athena never sees, because Athena strips comments before it counts placeholders:
    `INVALID_PARAMETER_USAGE: Incorrect number of parameters: expected 6 but found 9`, on
    every quantitative datapoint, on the live path only.

    Nothing offline could have caught it. `RecordedBackend` does no binding at all, so the
    one code path that rewrites SQL had no test that ran it against a real query's text.
    """

    def test_a_marker_in_a_line_comment_is_left_alone(self) -> None:
        sql = "-- :tenant_id is the undertaking\nSELECT x FROM t WHERE id = :tenant_id"
        statement, values = _bind(sql, {"tenant_id": "helios"}, snapshot_id=None)
        assert statement.count("?") == 1
        assert values == ["helios"]
        assert ":tenant_id is the undertaking" in statement

    def test_a_marker_in_a_block_comment_is_left_alone(self) -> None:
        sql = "/* binds :period_start */ SELECT x FROM t WHERE d >= :period_start"
        statement, values = _bind(sql, {"period_start": "2026-01-01"}, snapshot_id=None)
        assert statement.count("?") == 1
        assert values == ["2026-01-01"]

    def test_a_marker_in_a_string_literal_is_left_alone(self) -> None:
        sql = "SELECT ':tenant_id' AS label FROM t WHERE id = :tenant_id"
        statement, values = _bind(sql, {"tenant_id": "aegis"}, snapshot_id=None)
        assert statement.count("?") == 1
        assert "':tenant_id'" in statement
        assert values == ["aegis"]

    def test_every_committed_query_binds_what_athena_will_count(self, repo_root) -> None:
        """Placeholders in the statement must equal the values passed beside it."""
        parameters = {
            "tenant_id": "helios",
            "period_start": "2026-01-01",
            "period_end": "2027-01-01",
        }
        for path in sorted((repo_root / "queries").rglob("*.sql")):
            statement, values = _bind(
                path.read_text(encoding="utf-8"), parameters, snapshot_id=None
            )
            stripped = re.sub(r"--[^\n]*|/\*.*?\*/", "", statement, flags=re.DOTALL)
            assert stripped.count("?") == len(values), path.name
