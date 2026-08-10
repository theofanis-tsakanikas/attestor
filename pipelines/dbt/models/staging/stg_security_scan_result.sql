-- Retrieval-scanner outcomes on the labelled corpus, one row per passage.
--
-- Rename and cast, nothing else. No filtering: deciding what is admissible belongs to the
-- gold layer and to the tests, so a rejected row lands in quarantine carrying the rule it
-- broke rather than disappearing behind a WHERE clause nobody reads.
--
-- The labels are lower-cased here rather than trusted as written. A `Benign` that arrived
-- capitalised would silently fall out of the denominator of the false-positive rate and make
-- the figure look better, which is the direction an error in a safety metric must never go
-- undetected.

{{ config(materialized='view') }}

SELECT
    CAST(tenant_id AS VARCHAR) AS tenant_id,
    CAST(assessed_at AS DATE) AS assessed_at,
    CAST(example_id AS VARCHAR) AS example_id,
    LOWER(CAST(corpus AS VARCHAR)) AS corpus,
    LOWER(CAST(true_label AS VARCHAR)) AS true_label,
    LOWER(CAST(predicted_label AS VARCHAR)) AS predicted_label,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'security_scan_result') }}
