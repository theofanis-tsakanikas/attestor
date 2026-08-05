-- The table `queries/esrs/e1_5_electricity_consumption.sql` reads.
--
-- `dq_status` is computed here rather than assumed. A row is clean when it appears in no
-- quarantine table; the resolver counts the rest and refuses the figure rather than summing
-- around them. Doing this at the gold layer keeps the resolver's query simple and keeps the
-- decision about admissibility in one place.

{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['tenant_id', 'site_id', 'reading_date'],
    file_format='iceberg'
) }}

WITH quarantined AS (
    {{ quarantined_keys('stg_electricity') }}
)

SELECT
    s.tenant_id,
    s.site_id,
    s.reading_date,
    s.kwh,
    s.reading_type,
    s.source_document_id,
    CASE WHEN q.row_key IS NULL THEN 'clean' ELSE 'quarantined' END AS dq_status
FROM {{ ref('stg_electricity') }} AS s
LEFT JOIN quarantined AS q
    ON q.row_key = {{ row_key(['s.tenant_id', 's.site_id', 's.reading_date']) }}

{% if is_incremental() %}
    WHERE s.ingested_at > (SELECT COALESCE(MAX(ingested_at), TIMESTAMP '1970-01-01') FROM {{ this }})
{% endif %}
