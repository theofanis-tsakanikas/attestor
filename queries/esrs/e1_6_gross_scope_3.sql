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
    SUM(a.co2e_tonnes) AS value
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
