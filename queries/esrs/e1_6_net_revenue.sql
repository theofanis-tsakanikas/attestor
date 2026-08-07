-- ESRS_E1-6_net_revenue · net revenue, MEUR, as reported in the financial statements
--
-- Read from the posted general ledger, restricted to revenue accounts and to periods the
-- finance team has closed. An open period is not a reported figure, and a sustainability
-- statement that quotes an unclosed number will not survive its first reconciliation.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    SUM(l.amount_eur) / 1000000.0 AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it. Without this column the Athena
    -- backend had nothing to record and reproducibility held only in replay.
    (
        SELECT CAST(MAX(s.snapshot_id) AS VARCHAR)
        FROM "gold"."general_ledger_posting$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.general_ledger_posting AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.posting_date >= CAST(:period_start AS DATE)
            AND q.posting_date < CAST(:period_end AS DATE)
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.general_ledger_posting AS l
INNER JOIN ref.chart_of_accounts AS c
    ON
        c.tenant_id = l.tenant_id
        AND c.account_code = l.account_code
WHERE
    l.tenant_id = :tenant_id
    AND l.posting_date >= CAST(:period_start AS DATE)
    AND l.posting_date < CAST(:period_end AS DATE)
    AND c.account_class = 'net_revenue'
    AND l.period_status = 'closed'
    AND l.dq_status = 'clean'
