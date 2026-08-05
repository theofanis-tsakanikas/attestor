-- Rename, cast, and mark. No filtering: deciding what is admissible belongs to the tests, so
-- that a rejected row lands in quarantine with the rule it broke rather than disappearing
-- behind a WHERE clause nobody reads.

{{ config(materialized='view') }}

SELECT
    CAST(tenant_id AS VARCHAR) AS tenant_id,
    CAST(site_id AS VARCHAR) AS site_id,
    CAST(reading_date AS DATE) AS reading_date,
    CAST(kwh AS DECIMAL(18, 4)) AS kwh,
    LOWER(CAST(reading_type AS VARCHAR)) AS reading_type,
    CAST(source_document_id AS VARCHAR) AS source_document_id,
    CAST(ingested_at AS TIMESTAMP) AS ingested_at
FROM {{ source('raw', 'electricity_invoice') }}
