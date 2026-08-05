-- ESRS_E1-5_electricity_consumption · purchased electricity consumed, MWh
--
-- Only actual readings. An estimated invoice line is real data about a bill, but it is not
-- a measurement of consumption, and ESRS E1-5 asks for consumption. Estimates are excluded
-- here and reported as a coverage gap rather than blended in — which is why the site
-- coverage check below exists at all.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    SUM(e.kwh) / 1000.0 AS value
FROM gold.electricity_consumption AS e
WHERE
    e.tenant_id = :tenant_id
    AND e.reading_date >= :period_start
    AND e.reading_date < :period_end
    AND e.reading_type = 'actual'
    AND e.dq_status = 'clean'
