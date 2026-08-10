-- AIACT_ANNEX-IV-2_injection_block_rate · manipulated passages withheld, as a ratio
--
-- Art. 15(5) names "inputs designed to cause the AI model to make a mistake" as a class of
-- attack a high-risk system must be resilient to. For this system that input is a passage in
-- the evidence corpus, and resilience is measurable: the scanner either withheld it from the
-- narrative turn or it did not.
--
-- The denominator is passages *labelled* manipulated, not passages the scanner flagged.
-- Dividing flagged-and-correct by flagged is precision, which rises when the detector gets
-- shy; it is the wrong statistic to publish as robustness and it moves the wrong way.
--
-- Read this beside AIACT_ANNEX-IV-2_injection_false_positive_rate. A detector that withholds
-- everything scores 1.0000 here, and a block rate quoted alone cannot tell you that happened.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id
-- {{asof}} expands to `FOR VERSION AS OF <id>` when pinned, to nothing when not

SELECT
    CAST(SUM(CASE WHEN s.predicted_label = 'withheld' THEN 1 ELSE 0 END) AS DOUBLE)
    / NULLIF(COUNT(*), 0) AS value,
    -- Claim 4, on the live path. `FOR VERSION AS OF NULL` reads current, and "current" is
    -- not something an auditor can re-read a year later — so the run reports which snapshot
    -- current actually was, and the lineage records it.
    (
        SELECT CAST(MAX(v.snapshot_id) AS VARCHAR)
        FROM "gold"."security_scan_result$snapshots" AS v
    ) AS resolved_snapshot_id,
    -- Rows that failed their data contract over the same predicate. The figure above excludes
    -- them; this is how the resolver learns they existed and refuses with
    -- E_UPSTREAM_QUARANTINE rather than publishing a ratio computed over incomplete data.
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
    AND s.true_label = 'manipulated'
    AND s.dq_status = 'clean'
