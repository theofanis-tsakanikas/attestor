-- The independent meter feed E1-5 cross-checks the invoice total against.
--
-- The table the resolver queries read. `dq_status` is decided here and nowhere else, so
-- every query can say `dq_status = 'clean'` and mean the same thing.
--
-- Two inputs, and a row is dirty if either says so. The ingestion may already have
-- marked it; our own data-contract tests may have rejected it. Taking only the second
-- would discard what the evidence pipeline knew, and taking only the first would make
-- the tests decorative.

{{ config(materialized='table', file_format='iceberg') }}

WITH quarantined AS (
    {{ quarantined_keys('stg_meter_interval_reading') }}
)

SELECT
    s.tenant_id,
    -- `TIMESTAMP(6)` here and not in staging: this is the Iceberg table, and Iceberg
    -- refuses both the zoned `timestamp(3)` Athena produces and the bare Hive
    -- `timestamp` the view carries.
    CAST(s.interval_start AS TIMESTAMP(6)) AS interval_start,
    s.kwh,
    CASE
        WHEN q.row_key IS NOT NULL THEN 'quarantined'
        WHEN s.upstream_dq_status <> 'clean' THEN 'quarantined'
        ELSE 'clean'
    END AS dq_status
FROM {{ ref('stg_meter_interval_reading') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.interval_start', 's.kwh']) }}
