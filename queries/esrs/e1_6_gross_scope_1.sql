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
-- {{asof}}       expands to `FOR VERSION AS OF <id>` when pinned, to nothing when not

SELECT
    SUM(t.co2e_tonnes) AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it. Without this column the Athena
    -- backend had nothing to record and reproducibility held only in replay.
    (
        SELECT CAST(MAX(s.snapshot_id) AS VARCHAR)
        FROM "gold"."ghg_scope_1_activity$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.ghg_scope_1_activity AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.activity_date >= CAST(:period_start AS DATE)
            AND q.activity_date < CAST(:period_end AS DATE)
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
-- Reads the snapshot this run is pinned to, and reports which one that was through
-- `resolved_snapshot_id` above — so a run that read "current" records what current turned out
-- to be, and a replay of it reads exactly that.
--
-- This line used to say `FOR VERSION AS OF COALESCE(:snapshot_id, gold.ghg_scope_1_activity$
-- current_snapshot)`, which Athena rejects twice over: the clause takes a literal snapshot id
-- rather than an expression, and `table$current_snapshot` is not a value — the metadata tables
-- are relations you select from. It failed with `mismatched input '$'`, and for weeks this
-- comment said as-of pinning was simply not implemented on the live path.
--
-- `{{asof}}` is the answer to both objections. It is expanded by `_bind` into a literal or into
-- nothing at all, outside comments and string literals, and the snapshot id is validated as
-- digits before it is ever substituted — it is the one value in this system that reaches a
-- statement by substitution rather than by binding, and it earns that check.
FROM gold.ghg_scope_1_activity {{asof}} AS t
WHERE
    t.tenant_id = :tenant_id
    AND t.activity_date >= CAST(:period_start AS DATE)
    AND t.activity_date < CAST(:period_end AS DATE)
    AND t.consolidation_boundary = 'operational_control'
    -- Quarantined rows never reach a disclosure. They are not zero, they are absent, and
    -- their absence is reported as E_UPSTREAM_QUARANTINE rather than silently summed over.
    AND t.dq_status = 'clean'
