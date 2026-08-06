-- Revenue postings. `period_status` is what makes an open period a reason to abstain.
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
    CAST(posting_date AS DATE) AS posting_date,
    CAST(account_code AS VARCHAR) AS account_code,
    CAST(amount_eur AS DECIMAL(18, 2)) AS amount_eur,
    LOWER(CAST(period_status AS VARCHAR)) AS period_status,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'general_ledger_posting') }}
