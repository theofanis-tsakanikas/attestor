-- AIACT_ART-9_open_residual_risks · risks mitigated, with an accepted residual
--
-- Only entries whose mitigation is complete. A risk still under mitigation is not a residual
-- risk a deployer has been asked to accept, and folding the two together understates exactly
-- the thing Article 9(5) requires be communicated.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    COUNT(*) AS value
FROM gold.risk_register AS r
WHERE
    r.tenant_id = :tenant_id
    AND r.assessed_at >= :period_start
    AND r.assessed_at < :period_end
    AND r.mitigation_status = 'complete'
    AND r.residual_rating IN ('low', 'medium', 'high')
    AND r.dq_status = 'clean'
