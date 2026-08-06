-- One row per evaluated example. `is_held_out` is what keeps Annex IV accuracy honest.
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
    CAST(evaluated_at AS DATE) AS evaluated_at,
    CAST(example_id AS VARCHAR) AS example_id,
    LOWER(CAST(predicted_label AS VARCHAR)) AS predicted_label,
    LOWER(CAST(true_label AS VARCHAR)) AS true_label,
    CAST(is_held_out AS BOOLEAN) AS is_held_out,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'model_evaluation_prediction') }}
