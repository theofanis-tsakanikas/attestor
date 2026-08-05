-- AIACT_ANNEX-IV-8_serious_incidents · serious incidents under Article 73
--
-- Incidents still under investigation are counted. Waiting for classification to complete
-- before counting one would let a serious incident go unreported for as long as the review
-- takes, which is the opposite of what post-market monitoring is for.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    COUNT(*) AS value
FROM gold.incident_log AS i
WHERE
    i.tenant_id = :tenant_id
    AND i.occurred_at >= :period_start
    AND i.occurred_at < :period_end
    AND i.classification IN ('serious', 'under_investigation')
    AND i.dq_status = 'clean'
