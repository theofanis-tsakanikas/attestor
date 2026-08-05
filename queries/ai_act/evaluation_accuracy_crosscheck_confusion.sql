-- Cross-check for AIACT_ANNEX-IV-2_evaluation_accuracy, from the confusion matrix.
--
-- The primary query counts matching predictions row by row. This one starts from the
-- aggregated matrix the evaluation harness emits. They are the same number computed at two
-- levels of aggregation, so the bound is 0.01% rather than a tolerance — a real difference
-- means the matrix and the predictions come from different runs, which is a filing problem
-- and not a rounding one.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

SELECT
    CAST(SUM(c.count) FILTER (WHERE c.predicted_label = c.true_label) AS DOUBLE)
    / NULLIF(SUM(c.count), 0) AS value
FROM gold.model_evaluation_confusion AS c
WHERE
    c.tenant_id = :tenant_id
    AND c.evaluated_at >= :period_start
    AND c.evaluated_at < :period_end
    AND c.dq_status = 'clean'
