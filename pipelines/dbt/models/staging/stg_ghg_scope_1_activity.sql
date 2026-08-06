-- Direct emissions. `consolidation_boundary` decides which rows belong to the undertaking.
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
    CAST(activity_date AS DATE) AS activity_date,
    CAST(co2e_tonnes AS DECIMAL(18, 4)) AS co2e_tonnes,
    LOWER(CAST(consolidation_boundary AS VARCHAR)) AS consolidation_boundary,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'ghg_scope_1_activity') }}
