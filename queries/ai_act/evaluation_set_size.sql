-- AIACT_ANNEX-IV-2_evaluation_set_size · how many examples the accuracy was measured on
--
-- Distinct example ids, not row count. A duplicated example is one example evaluated twice,
-- and counting it twice makes a small evaluation set look adequate.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    COUNT(DISTINCT p.example_id) AS value
FROM gold.model_evaluation_prediction AS p
WHERE
    p.tenant_id = :tenant_id
    AND p.evaluated_at >= :period_start
    AND p.evaluated_at < :period_end
    AND p.is_held_out = TRUE
    AND p.dq_status = 'clean'
