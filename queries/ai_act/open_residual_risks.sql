-- AIACT_ART-9_open_residual_risks · risks mitigated, with an accepted residual
--
-- Only entries whose mitigation is complete. A risk still under mitigation is not a residual
-- risk a deployer has been asked to accept, and folding the two together understates exactly
-- the thing Article 9(5) requires be communicated.
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
        FROM "gold"."risk_register$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.risk_register AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.assessed_at >= :period_start
            AND q.assessed_at < :period_end
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.risk_register AS r
WHERE
    r.tenant_id = :tenant_id
    AND r.assessed_at >= :period_start
    AND r.assessed_at < :period_end
    AND r.mitigation_status = 'complete'
    AND r.residual_rating IN ('low', 'medium', 'high')
    AND r.dq_status = 'clean'
