-- One row per rejected record, carrying the rule it violated.
--
-- This is the table that makes `E_UPSTREAM_QUARANTINE` an honest answer. Without it the
-- resolver could only say "the figure is smaller than expected"; with it, it can say which
-- rows were excluded, by which contract, and a human can decide whether the gap is material.
--
-- Nothing is ever deleted from here. A quarantined row that was later fixed upstream appears
-- twice with different ingest timestamps, and the history of what was wrong is itself
-- evidence.
--
-- The guard below is not defensive clutter, it is the run order. `all_failures` is assembled
-- by `build_quarantine_view` in `on-run-end`, which by definition runs *after* every model —
-- so on a fresh estate this model is the first thing dbt builds and the view it reads does
-- not exist yet. The first live build failed exactly there:
-- `TABLE_NOT_FOUND ... attestor_gold.all_failures`, with the other 55 nodes skipped behind it.
--
-- `quarantined_keys` already degrades this way for the same reason. Absence means "no test has
-- stored a failure", which is true before any test has run, and the empty branch keeps the
-- column types so the table is created with the right shape either way.

{{ config(materialized='incremental', incremental_strategy='append', file_format='iceberg') }}

{% set failures = adapter.get_relation(
    database=target.database, schema=target.schema, identifier='all_failures'
) %}

{% if failures %}

SELECT
    f.tenant_id,
    f.model_name AS source_table,
    f.rule,
    f.row_key,
    f.payload,
    -- `TIMESTAMP(6)`, not bare `CURRENT_TIMESTAMP`. Athena hands back
    -- `timestamp(3) with time zone` and Iceberg refuses it outright:
    -- `NOT_SUPPORTED: Unsupported Hive type: timestamp(3) with time zone`.
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6)) AS quarantined_at
FROM {{ target.schema }}.all_failures AS f

{% if is_incremental() %}
    WHERE f.detected_at > (SELECT COALESCE(MAX(quarantined_at), TIMESTAMP '1970-01-01') FROM {{ this }})
{% endif %}

{% else %}

SELECT
    CAST(NULL AS VARCHAR) AS tenant_id,
    CAST(NULL AS VARCHAR) AS source_table,
    CAST(NULL AS VARCHAR) AS rule,
    CAST(NULL AS VARCHAR) AS row_key,
    CAST(NULL AS VARCHAR) AS payload,
    CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6)) AS quarantined_at
WHERE 1 = 0

{% endif %}
