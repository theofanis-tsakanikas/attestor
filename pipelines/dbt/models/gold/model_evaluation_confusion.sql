-- The confusion matrix the accuracy figure is cross-checked against.
--
-- The table the resolver queries read. `dq_status` is decided here and nowhere else, so
-- every query can say `dq_status = 'clean'` and mean the same thing.
--
-- Two inputs, and a row is dirty if either says so. The ingestion may already have
-- marked it; our own data-contract tests may have rejected it. Taking only the second
-- would discard what the evidence pipeline knew, and taking only the first would make
-- the tests decorative.

{{ config(materialized='table', table_type='iceberg') }}

WITH quarantined AS (
    {{ quarantined_keys('stg_model_evaluation_confusion') }}
)

SELECT
    s.tenant_id,
    s.evaluated_at,
    s.predicted_label,
    s.true_label,
    s.count,
    CASE
        WHEN q.row_key IS NOT NULL THEN 'quarantined'
        WHEN s.upstream_dq_status <> 'clean' THEN 'quarantined'
        ELSE 'clean'
    END AS dq_status
FROM {{ ref('stg_model_evaluation_confusion') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.evaluated_at', 's.predicted_label']) }}
