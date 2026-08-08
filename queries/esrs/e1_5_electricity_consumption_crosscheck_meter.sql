-- Cross-check for ESRS_E1-5_electricity_consumption.
--
-- The primary query sums invoice lines. This one sums half-hourly meter telemetry, which
-- arrives from a different system on a different schedule. Invoices and meters disagreeing
-- by more than 1% usually means a site changed supplier mid-period and one of the two
-- feeds lost it — a coverage bug that looks like nothing at all in the total.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id
-- {{asof}} expands to `FOR VERSION AS OF <id>` when pinned, to nothing when not

SELECT
    SUM(m.kwh) / 1000.0 AS value
FROM gold.meter_interval_reading {{asof}} AS m
WHERE
    m.tenant_id = :tenant_id
    AND m.interval_start >= CAST(:period_start AS DATE)
    AND m.interval_start < CAST(:period_end AS DATE)
    AND m.dq_status = 'clean'
