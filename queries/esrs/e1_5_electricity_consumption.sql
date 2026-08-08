-- ESRS_E1-5_electricity_consumption · purchased electricity consumed, MWh
--
-- Only actual readings. An estimated invoice line is real data about a bill, but it is not
-- a measurement of consumption, and ESRS E1-5 asks for consumption. Estimates are excluded
-- here and reported as a coverage gap rather than blended in — which is why the site
-- coverage check below exists at all.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id
-- {{asof}} expands to `FOR VERSION AS OF <id>` when pinned, to nothing when not

SELECT
    SUM(e.kwh) / 1000.0 AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it. Without this column the Athena
    -- backend had nothing to record and reproducibility held only in replay.
    (
        SELECT CAST(MAX(s.snapshot_id) AS VARCHAR)
        FROM "gold"."electricity_consumption$snapshots" AS s
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a total computed over incomplete data.
    (
        SELECT COUNT(*)
        FROM gold.electricity_consumption AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.reading_date >= CAST(:period_start AS DATE)
            AND q.reading_date < CAST(:period_end AS DATE)
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.electricity_consumption {{asof}} AS e
WHERE
    e.tenant_id = :tenant_id
    AND e.reading_date >= CAST(:period_start AS DATE)
    AND e.reading_date < CAST(:period_end AS DATE)
    AND e.reading_type = 'actual'
    AND e.dq_status = 'clean'
