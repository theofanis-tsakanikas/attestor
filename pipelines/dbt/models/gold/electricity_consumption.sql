-- Metered electricity, one row per site per reading date.
--
-- The table the resolver queries read. `dq_status` is decided here and nowhere else, so
-- every query can say `dq_status = 'clean'` and mean the same thing.
--
-- Two inputs, and a row is dirty if either says so. The ingestion may already have
-- marked it; our own data-contract tests may have rejected it. Taking only the second
-- would discard what the evidence pipeline knew, and taking only the first would make
-- the tests decorative.

-- `table_type`, not `file_format`. `file_format` is a Spark config key and dbt-athena
-- ignores it silently, so every gold table was built as **Hive** while the config said
-- Iceberg. It looked right for as long as nobody asked for a snapshot: the resolver's
-- `resolved_snapshot_id` reads `"gold"."<table>$snapshots"`, a metadata relation only
-- an Iceberg table has, and the live run failed with `TABLE_NOT_FOUND ... $snapshots`.
--
-- Claim 4 is a table-format property before it is an application property, and a config
-- key with a typo in it is not a table format.
{{ config(materialized='table', table_type='iceberg') }}

WITH quarantined AS (
    {{ quarantined_keys('stg_electricity_consumption') }}
)

SELECT
    s.tenant_id,
    s.reading_date,
    s.kwh,
    s.reading_type,
    CASE
        WHEN q.row_key IS NOT NULL THEN 'quarantined'
        WHEN s.upstream_dq_status <> 'clean' THEN 'quarantined'
        ELSE 'clean'
    END AS dq_status
FROM {{ ref('stg_electricity_consumption') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.reading_date', 's.kwh']) }}
