{{ config(materialized='view') }}

SELECT
    CAST(tenant_id AS VARCHAR) AS tenant_id,
    CAST(vehicle_id AS VARCHAR) AS vehicle_id,
    CAST(transaction_date AS DATE) AS transaction_date,
    CAST(litres AS DECIMAL(18, 4)) AS litres,
    LOWER(CAST(fuel_type AS VARCHAR)) AS fuel_type,
    CAST(net_amount_eur AS DECIMAL(18, 2)) AS net_amount_eur,
    CAST(source_document_id AS VARCHAR) AS source_document_id
FROM {{ source('raw', 'fuel_transaction') }}
