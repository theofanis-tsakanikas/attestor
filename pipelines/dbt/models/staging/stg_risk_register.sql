-- Annex IV residual risks. An unmitigated high rating is a disclosure, not a defect.
--
-- Rename and cast, nothing else. No filtering: deciding what is admissible belongs to
-- the gold layer and to the tests, so a rejected row lands in quarantine carrying the
-- rule it broke rather than disappearing behind a WHERE clause nobody reads.
--
-- `upstream_dq_status` is the ingestion's own verdict, carried forward under a name that
-- says whose verdict it is. Dropping it would silently promote every row the evidence
-- pipeline had already marked as suspect.

{{ config(materialized='view') }}

SELECT
    CAST(tenant_id AS VARCHAR) AS tenant_id,
    CAST(assessed_at AS DATE) AS assessed_at,
    CAST(risk_id AS VARCHAR) AS risk_id,
    LOWER(CAST(mitigation_status AS VARCHAR)) AS mitigation_status,
    LOWER(CAST(residual_rating AS VARCHAR)) AS residual_rating,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'risk_register') }}
