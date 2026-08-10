-- AIACT_ANNEX-IV-2_injection_false_positive_rate · benign passages wrongly withheld
--
-- The other half of the robustness figure, and the half a vendor omits. A detector that
-- withholds benign evidence does not fail safe: it starves the disclosure of the material an
-- undertaking is required to disclose, and the system then abstains for a reason that is an
-- artefact of its own defences rather than a fact about the corpus.
--
-- Benign here does not mean unrelated prose. The labelled benign passages are the ones a
-- careless detector fails — correspondence quoting an instruction, a methodology note
-- describing what the system must not do. Measured against inert text this number would be
-- zero and would mean nothing.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id
-- {{asof}} expands to `FOR VERSION AS OF <id>` when pinned, to nothing when not

SELECT
    CAST(SUM(CASE WHEN s.predicted_label = 'withheld' THEN 1 ELSE 0 END) AS DOUBLE)
    / NULLIF(COUNT(*), 0) AS value,
    (
        SELECT CAST(MAX(v.snapshot_id) AS VARCHAR)
        FROM "gold"."security_scan_result$snapshots" AS v
    ) AS resolved_snapshot_id,
    (
        SELECT COUNT(*)
        FROM gold.security_scan_result AS q
        WHERE
            q.tenant_id = :tenant_id
            AND q.assessed_at >= CAST(:period_start AS DATE)
            AND q.assessed_at < CAST(:period_end AS DATE)
            AND q.corpus = 'injection'
            AND q.dq_status <> 'clean'
    ) AS quarantined_rows
FROM gold.security_scan_result {{asof}} AS s
WHERE
    s.tenant_id = :tenant_id
    AND s.assessed_at >= CAST(:period_start AS DATE)
    AND s.assessed_at < CAST(:period_end AS DATE)
    AND s.corpus = 'injection'
    AND s.true_label = 'benign'
    AND s.dq_status = 'clean'
