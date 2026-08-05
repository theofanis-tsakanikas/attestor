-- ESRS_E1-6_net_revenue · net revenue, MEUR, as reported in the financial statements
--
-- Read from the posted general ledger, restricted to revenue accounts and to periods the
-- finance team has closed. An open period is not a reported figure, and a sustainability
-- statement that quotes an unclosed number will not survive its first reconciliation.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    SUM(l.amount_eur) / 1000000.0 AS value
FROM gold.general_ledger_posting AS l
INNER JOIN ref.chart_of_accounts AS c
    ON
        c.tenant_id = l.tenant_id
        AND c.account_code = l.account_code
WHERE
    l.tenant_id = :tenant_id
    AND l.posting_date >= :period_start
    AND l.posting_date < :period_end
    AND c.account_class = 'net_revenue'
    AND l.period_status = 'closed'
    AND l.dq_status = 'clean'
