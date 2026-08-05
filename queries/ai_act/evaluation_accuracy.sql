-- AIACT_ANNEX-IV-2_evaluation_accuracy · accuracy on the held-out set
--
-- Recomputed from the evaluation run's own predictions rather than read from a summary. A
-- headline accuracy in a report is a number somebody typed; this is a number the run
-- produced, and the two disagree more often than anyone expects.
--
-- `is_held_out` is not decoration. An example that appears in training and in evaluation
-- inflates this figure silently, and it is the single most common way a model's documented
-- accuracy is wrong without anybody lying.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    CAST(SUM(CASE WHEN p.predicted_label = p.true_label THEN 1 ELSE 0 END) AS DOUBLE)
    / NULLIF(COUNT(*), 0) AS value
FROM gold.model_evaluation_prediction AS p
WHERE
    p.tenant_id = :tenant_id
    AND p.evaluated_at >= :period_start
    AND p.evaluated_at < :period_end
    AND p.is_held_out = TRUE
    AND p.dq_status = 'clean'
