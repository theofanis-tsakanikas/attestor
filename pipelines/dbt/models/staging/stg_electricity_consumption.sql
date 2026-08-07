-- Metered electricity, one row per site per reading date.
--
-- Rename and cast, nothing else. No filtering: deciding what is admissible belongs to
-- the gold layer and to the tests, so a rejected row lands in quarantine carrying the
-- rule it broke rather than disappearing behind a WHERE clause nobody reads.
--
-- `upstream_dq_status` is the ingestion's own verdict, carried forward under a name that
-- says whose verdict it is. Dropping it would silently promote every row the evidence
-- pipeline had already marked as suspect.

-- Plain `TIMESTAMP`, because this model is a **view** and a Hive view rejects
-- `timestamp(6)`: `Invalid column type for column ...: Unsupported Hive type:
-- timestamp(6)`. The gold models are Iceberg and want the opposite — the precision is
-- added there, at the layer that stores it. One rule per materialisation, not one rule.

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
