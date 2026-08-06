-- The filed statement the ledger total is reconciled against.
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
    CAST(period_start AS DATE) AS period_start,
    CAST(period_end AS DATE) AS period_end,
    CAST(net_revenue_eur AS DECIMAL(18, 2)) AS net_revenue_eur,
    LOWER(CAST(statement_status AS VARCHAR)) AS statement_status,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'financial_statement_extract') }}
