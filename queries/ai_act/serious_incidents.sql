-- AIACT_ANNEX-IV-8_serious_incidents · serious incidents under Article 73
--
-- Incidents still under investigation are counted. Waiting for classification to complete
-- before counting one would let a serious incident go unreported for as long as the review
-- takes, which is the opposite of what post-market monitoring is for.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    COUNT(*) AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it. Without this column the Athena
    -- backend had nothing to record and reproducibility held only in replay.
    (
        SELECT CAST(MAX(s.snapshot_id) AS VARCHAR)
        FROM "gold"."incident_log$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.incident_log AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.occurred_at >= CAST(:period_start AS DATE)
            AND q.occurred_at < CAST(:period_end AS DATE)
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.incident_log AS i
WHERE
    i.tenant_id = :tenant_id
    AND i.occurred_at >= CAST(:period_start AS DATE)
    AND i.occurred_at < CAST(:period_end AS DATE)
    AND i.classification IN ('serious', 'under_investigation')
    AND i.dq_status = 'clean'
