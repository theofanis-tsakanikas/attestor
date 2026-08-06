-- Serious incidents. Deliberately empty in the seeded lake: this is the table that makes claim 5 real, because a datapoint with no evidence must be abstained from rather than answered with a zero.
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
    CAST(occurred_at AS DATE) AS occurred_at,
    CAST(incident_id AS VARCHAR) AS incident_id,
    LOWER(CAST(classification AS VARCHAR)) AS classification,
    LOWER(CAST(dq_status AS VARCHAR)) AS upstream_dq_status
FROM {{ source('raw', 'incident_log') }}
