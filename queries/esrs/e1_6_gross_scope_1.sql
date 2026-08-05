-- ESRS_E1-6_gross_scope_1 · gross Scope 1 greenhouse gas emissions
--
-- Parameters are bound, never interpolated. `tenant_id` in particular: a query that builds
-- its own tenant predicate by string concatenation is one typo away from summing two
-- clients' emissions into one disclosure, and no reviewer would see it.
--
-- The snapshot pin is what makes claim 4 work. Every resolution records the Iceberg snapshot
-- it read, and re-resolving as-of that snapshot returns the same rows even after the table
-- has moved on.
--
-- :tenant_id      the undertaking (bound, scoped again by Lake Formation)
-- :period_start   inclusive
-- :period_end     exclusive
-- :snapshot_id    Iceberg snapshot to read; NULL reads current and records what it read

SELECT
    SUM(t.co2e_tonnes) AS value
FROM gold.ghg_scope_1_activity FOR VERSION AS OF COALESCE(:snapshot_id, gold.ghg_scope_1_activity$current_snapshot) AS t
WHERE
    t.tenant_id = :tenant_id
    AND t.activity_date >= :period_start
    AND t.activity_date < :period_end
    AND t.consolidation_boundary = 'operational_control'
    -- Quarantined rows never reach a disclosure. They are not zero, they are absent, and
    -- their absence is reported as E_UPSTREAM_QUARANTINE rather than silently summed over.
    AND t.dq_status = 'clean'
