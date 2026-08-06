-- The confusion matrix the accuracy figure is cross-checked against.
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
    LOWER(CAST(predicted_label AS VARCHAR)) AS predicted_label,
    LOWER(CAST(true_label AS VARCHAR)) AS true_label,
    CAST(count AS BIGINT) AS count,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'model_evaluation_confusion') }}
