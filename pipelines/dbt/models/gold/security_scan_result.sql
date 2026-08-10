-- Retrieval-scanner outcomes on the labelled corpus, one row per passage.
--
-- The table the robustness resolvers read. `dq_status` is decided here and nowhere else, so
-- every query can say `dq_status = 'clean'` and mean the same thing.
--
-- Two inputs, and a row is dirty if either says so. The ingestion may already have marked it;
-- our own data-contract tests may have rejected it. Taking only the second would discard what
-- the pipeline knew, and taking only the first would make the tests decorative.
--
-- A quarantined row here does not merely dirty a total, it dirties a *ratio* — and a ratio
-- computed over a subset of its labelled set is wrong in a way that looks fine. That is why
-- both queries over this table count the quarantined rows and refuse rather than divide.

{{ config(materialized='table', table_type='iceberg') }}

WITH quarantined AS (
    {{ quarantined_keys('stg_security_scan_result') }}
)

SELECT
    s.tenant_id,
    s.assessed_at,
    s.example_id,
    s.corpus,
    s.true_label,
    s.predicted_label,
    CASE
        WHEN q.row_key IS NOT NULL THEN 'quarantined'
        WHEN s.upstream_dq_status <> 'clean' THEN 'quarantined'
        ELSE 'clean'
    END AS dq_status
FROM {{ ref('stg_security_scan_result') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.assessed_at', 's.example_id']) }}
