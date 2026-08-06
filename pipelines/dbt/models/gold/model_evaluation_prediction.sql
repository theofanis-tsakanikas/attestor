-- One row per evaluated example. `is_held_out` is what keeps Annex IV accuracy honest.
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
    {{ quarantined_keys('stg_model_evaluation_prediction') }}
)

SELECT
    s.tenant_id,
    s.evaluated_at,
    s.example_id,
    s.predicted_label,
    s.true_label,
    s.is_held_out,
    CASE
        WHEN q.row_key IS NOT NULL THEN 'quarantined'
        WHEN s.upstream_dq_status <> 'clean' THEN 'quarantined'
        ELSE 'clean'
    END AS dq_status
FROM {{ ref('stg_model_evaluation_prediction') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.evaluated_at', 's.example_id']) }}
