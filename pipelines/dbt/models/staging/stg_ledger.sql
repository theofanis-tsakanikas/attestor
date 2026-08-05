{{ config(materialized='view') }}

SELECT
    CAST(tenant_id AS VARCHAR) AS tenant_id,
    CAST(posting_date AS DATE) AS posting_date,
    CAST(account_code AS VARCHAR) AS account_code,
    CAST(amount_eur AS DECIMAL(18, 2)) AS amount_eur,
    LOWER(CAST(period_status AS VARCHAR)) AS period_status
FROM {{ source('raw', 'general_ledger') }}
