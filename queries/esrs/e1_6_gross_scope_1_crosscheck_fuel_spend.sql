-- Cross-check for ESRS_E1-6_gross_scope_1, by an independent route.
--
-- The primary query sums metered and telematics-derived fuel volumes. This one starts from
-- procurement spend, converts to volume at the period's average price, and applies the same
-- factors. The two should land within 0.5%.
--
-- The point is not precision. It is that two different source systems, maintained by two
-- different teams, have to agree before a figure is published. A single-source figure is a
-- figure nobody has ever verified.
--
-- :tenant_id · :period_start · :period_end · :snapshot_id

WITH spend AS (
    SELECT
        p.fuel_type,
        SUM(p.net_amount_eur) AS spend_eur
    FROM gold.procurement_fuel_spend AS p
    WHERE
        p.tenant_id = :tenant_id
        AND p.invoice_date >= :period_start
        AND p.invoice_date < :period_end
        AND p.dq_status = 'clean'
    GROUP BY p.fuel_type
),

price AS (
    SELECT
        r.fuel_type,
        r.avg_price_eur_per_litre
    FROM ref.fuel_price_period AS r
    WHERE
        r.period_start = :period_start
        AND r.period_end = :period_end
)

SELECT
    SUM((s.spend_eur / p.avg_price_eur_per_litre) * f.kg_co2e_per_litre / 1000.0) AS value
FROM spend AS s
INNER JOIN price AS p ON p.fuel_type = s.fuel_type
INNER JOIN ref.fuel_emission_factor AS f ON f.fuel_type = s.fuel_type
