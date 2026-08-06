-- Fuel invoices. Backs the Scope 1 cross-check through a price table, never the figure itself.
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
    CAST(invoice_date AS DATE) AS invoice_date,
    LOWER(CAST(fuel_type AS VARCHAR)) AS fuel_type,
    CAST(net_amount_eur AS DECIMAL(18, 2)) AS net_amount_eur,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'procurement_fuel_spend') }}
