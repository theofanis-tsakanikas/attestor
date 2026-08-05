-- The questions people actually ask after a run, as Athena views.
--
-- These are separate from `queries/`, and the separation matters. Everything in `queries/`
-- is a *resolver* query: it produces a published figure, it is tenant-scoped by a bound
-- parameter, and its text is hashed into a lineage record. Nothing here does. These read the
-- run records after the fact, they are not tenant-scoped by a parameter (Lake Formation and
-- the workgroup do that), and changing one restates nothing.
--
-- Applied by the deploy workflow after the data layer.

-- ── Did it issue, and what did it admit to? ─────────────────────────────────
CREATE OR REPLACE VIEW attestor_gold.v_run_summary AS
SELECT
    r.tenant_id,
    r.standard,
    r.period,
    r.run_id,
    r.started_at,
    r.issued,
    r.published_count,
    r.limitation_count,
    r.blocker_count,
    CAST(r.cost_eur AS DECIMAL(12, 4)) AS cost_eur,
    -- The proportion of what was owed that was actually disclosed. A single number that
    -- moves for two very different reasons — more evidence, or a looser gate — which is why
    -- it sits beside the blocker count rather than replacing it.
    CAST(r.published_count AS DOUBLE)
    / NULLIF(r.published_count + r.limitation_count + r.blocker_count, 0) AS disclosure_rate
FROM attestor_gold.report_run AS r
WHERE r.dq_status = 'clean';

-- ── What stopped us, how often, and for how long? ───────────────────────────
--
-- Grouped by reason code rather than by datapoint. A datapoint that blocks once is a bad
-- quarter; a reason code that blocks every quarter is a broken source system, and only the
-- second is actionable.
CREATE OR REPLACE VIEW attestor_gold.v_blocker_history AS
SELECT
    d.tenant_id,
    d.period,
    d.reason_code,
    COUNT(*) AS occurrences,
    COUNT(DISTINCT d.datapoint_id) AS distinct_datapoints,
    ARRAY_AGG(DISTINCT d.datapoint_id) AS datapoints
FROM attestor_gold.report_datapoint AS d
WHERE
    d.disclosed = FALSE
    AND d.outcome = 'blocked'
    AND d.dq_status = 'clean'
GROUP BY d.tenant_id, d.period, d.reason_code;

-- ── Which accepted defects are still standing? ──────────────────────────────
--
-- The register is the source of truth for *whether* a defect was accepted; this is where you
-- see how much of the report rests on those acceptances. A tenant whose disclosure rate is
-- healthy only because four overrides are live is a tenant with a cliff in its diary.
CREATE OR REPLACE VIEW attestor_gold.v_accepted_defects AS
SELECT
    d.tenant_id,
    d.period,
    d.datapoint_id,
    d.reason_code,
    d.outcome,
    COUNT(*) OVER (PARTITION BY d.tenant_id, d.period) AS accepted_in_period
FROM attestor_gold.report_datapoint AS d
WHERE
    d.disclosed = FALSE
    AND d.outcome <> 'blocked'
    AND d.dq_status = 'clean';

-- ── What does a report cost, per tenant? ────────────────────────────────────
--
-- Per tenant and per report, never as one total. A single monthly figure hides both the
-- customer who is unprofitable and the change that made everyone more expensive.
CREATE OR REPLACE VIEW attestor_gold.v_cost_per_report AS
SELECT
    r.tenant_id,
    r.standard,
    COUNT(*) AS runs,
    SUM(CAST(r.cost_eur AS DECIMAL(12, 4))) AS total_eur,
    AVG(CAST(r.cost_eur AS DECIMAL(12, 4))) AS avg_eur_per_run,
    SUM(CASE WHEN r.issued THEN 1 ELSE 0 END) AS issued_runs,
    -- Cost of the runs that produced nothing. A blocked run still spends: it resolved, it
    -- retrieved, it drafted, and then it refused. That number is worth watching.
    SUM(CASE WHEN r.issued THEN 0 ELSE CAST(r.cost_eur AS DECIMAL(12, 4)) END) AS wasted_eur
FROM attestor_gold.report_run AS r
WHERE r.dq_status = 'clean'
GROUP BY r.tenant_id, r.standard;

-- ── Can a figure be traced? ─────────────────────────────────────────────────
--
-- The auditor's query. Give it a datapoint and a period, get the lineage id, the resolver
-- and the source tables with their pinned snapshots.
CREATE OR REPLACE VIEW attestor_gold.v_lineage AS
SELECT
    d.tenant_id,
    d.period,
    d.run_id,
    d.datapoint_id,
    d.reference,
    d.value,
    d.unit,
    d.lineage_id,
    d.resolver_kind
FROM attestor_gold.report_datapoint AS d
WHERE
    d.disclosed = TRUE
    AND d.dq_status = 'clean';
