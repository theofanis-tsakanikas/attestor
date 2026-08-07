-- Revenue postings. `period_status` is what makes an open period a reason to abstain.
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
    {{ quarantined_keys('stg_general_ledger_posting') }}
)

SELECT
    s.tenant_id,
    s.posting_date,
    s.account_code,
    s.amount_eur,
    s.period_status,
    CASE
        WHEN q.row_key IS NOT NULL THEN 'quarantined'
        WHEN s.upstream_dq_status <> 'clean' THEN 'quarantined'
        ELSE 'clean'
    END AS dq_status
FROM {{ ref('stg_general_ledger_posting') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.posting_date', 's.account_code']) }}
