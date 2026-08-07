-- AIACT_ANNEX-IV-2_evaluation_set_size · how many examples the accuracy was measured on
--
-- Distinct example ids, not row count. A duplicated example is one example evaluated twice,
-- and counting it twice makes a small evaluation set look adequate.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    COUNT(DISTINCT p.example_id) AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it. Without this column the Athena
    -- backend had nothing to record and reproducibility held only in replay.
    (
        SELECT CAST(MAX(s.snapshot_id) AS VARCHAR)
        FROM "gold"."model_evaluation_prediction$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.model_evaluation_prediction AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.evaluated_at >= CAST(:period_start AS DATE)
            AND q.evaluated_at < CAST(:period_end AS DATE)
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.model_evaluation_prediction AS p
WHERE
    p.tenant_id = :tenant_id
    AND p.evaluated_at >= CAST(:period_start AS DATE)
    AND p.evaluated_at < CAST(:period_end AS DATE)
    AND p.is_held_out = TRUE
    AND p.dq_status = 'clean'
