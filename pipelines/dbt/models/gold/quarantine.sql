-- One row per rejected record, carrying the rule it violated.
--
-- This is the table that makes `E_UPSTREAM_QUARANTINE` an honest answer. Without it the
-- resolver could only say "the figure is smaller than expected"; with it, it can say which
-- rows were excluded, by which contract, and a human can decide whether the gap is material.
--
-- Nothing is ever deleted from here. A quarantined row that was later fixed upstream appears
-- twice with different ingest timestamps, and the history of what was wrong is itself
-- evidence.

{{ config(materialized='incremental', incremental_strategy='append', file_format='iceberg') }}

SELECT
    f.tenant_id,
    f.model_name AS source_table,
    f.rule,
    f.row_key,
    f.payload,
    CURRENT_TIMESTAMP AS quarantined_at
FROM {{ target.schema }}_quarantine.all_failures AS f

{% if is_incremental() %}
    WHERE f.detected_at > (SELECT COALESCE(MAX(quarantined_at), TIMESTAMP '1970-01-01') FROM {{ this }})
{% endif %}
