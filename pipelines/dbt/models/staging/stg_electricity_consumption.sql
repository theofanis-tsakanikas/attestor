-- Metered electricity, one row per site per reading date.
--
-- Rename and cast, nothing else. No filtering: deciding what is admissible belongs to
-- the gold layer and to the tests, so a rejected row lands in quarantine carrying the
-- rule it broke rather than disappearing behind a WHERE clause nobody reads.
--
-- `upstream_dq_status` is the ingestion's own verdict, carried forward under a name that
-- says whose verdict it is. Dropping it would silently promote every row the evidence
-- pipeline had already marked as suspect.

-- Plain `TIMESTAMP` throughout: milliseconds, no time zone. A Hive view rejects
-- `timestamp(6)` and an Iceberg table configured in MILLISECONDS rejects it too, so the
-- only thing that ever needed fixing was the *zone* Athena attaches to
-- `CURRENT_TIMESTAMP` and `FROM_ISO8601_TIMESTAMP`. The cast is what removes it.

{{ config(materialized='view') }}

SELECT
    CAST(tenant_id AS VARCHAR) AS tenant_id,
    CAST(reading_date AS DATE) AS reading_date,
    CAST(kwh AS DECIMAL(18, 4)) AS kwh,
    LOWER(CAST(reading_type AS VARCHAR)) AS reading_type,
    CAST(site_id AS VARCHAR) AS site_id,
    CAST(source_document_id AS VARCHAR) AS source_document_id,
    CAST(FROM_ISO8601_TIMESTAMP(ingested_at) AS TIMESTAMP) AS ingested_at,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'electricity_consumption') }}
