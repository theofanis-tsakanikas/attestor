-- ESRS_E1-6_gross_scope_3 · value-chain emissions across screened significant categories
--
-- Two things worth noticing.
--
-- First, the join to `ref.scope_3_category_screening` is inner, not left. A category that
-- was never screened does not silently contribute zero — it does not contribute at all, and
-- the coverage check downstream turns that into E_PARTIAL_BOUNDARY. Assuming zero for
-- unscreened value-chain activity is the single most common way a Scope 3 figure is
-- understated.
--
-- Second, `estimation_method` travels with the number. Supplier-specific data and
-- spend-based estimates are not the same quality of evidence, and the auditor annex says
-- which categories used which.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    SUM(a.co2e_tonnes) AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it. Without this column the Athena
    -- backend had nothing to record and reproducibility held only in replay.
    (
        SELECT CAST(MAX(s.snapshot_id) AS VARCHAR)
        FROM "gold"."ghg_scope_3_activity$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.ghg_scope_3_activity AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.activity_date >= :period_start
            AND q.activity_date < :period_end
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.ghg_scope_3_activity AS a
INNER JOIN ref.scope_3_category_screening AS s
    ON
        s.tenant_id = a.tenant_id
        AND s.category = a.category
        AND s.period_start = :period_start
WHERE
    a.tenant_id = :tenant_id
    AND a.activity_date >= :period_start
    AND a.activity_date < :period_end
    AND s.is_significant = TRUE
    AND a.dq_status = 'clean'
