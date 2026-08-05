-- Cross-check for ESRS_E1-6_net_revenue.
--
-- The primary query aggregates ledger postings. This one reads the filed statutory figure
-- from the financial-statement extract. ESRS E1-6 §55 requires the intensity denominator to
-- be *the* reported net revenue, so this is not really a tolerance check on arithmetic — it
-- is a check that the sustainability statement and the annual accounts are talking about
-- the same company in the same period.
--
-- The bound on this one is 0.01%, not 0.5%. These two numbers are not approximations of
-- each other; they are the same number, and a real difference means somebody restated.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    f.net_revenue_eur / 1000000.0 AS value
FROM gold.financial_statement_extract AS f
WHERE
    f.tenant_id = :tenant_id
    AND f.period_start = :period_start
    AND f.period_end = :period_end
    AND f.statement_status = 'filed'
    AND f.dq_status = 'clean'
