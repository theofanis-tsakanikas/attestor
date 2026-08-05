# ESRS E1-5 §37(a) — Total electricity consumption from purchased or acquired sources

- **standard**: ESRS (2023-12 (Delegated Regulation (EU) 2023/2772))
- **datapoint**: `ESRS_E1-5_electricity_consumption`
- **kind**: quantitative
- **reporting period basis**: fiscal_year
- **unit**: MWh (published to 1 dp)
- **consolidation boundary**: operational_control

## What the clause requires

Electricity purchased or otherwise acquired from third parties and consumed by the undertaking during the reporting period, metered at the point of delivery.

## Methodology as declared

Sum of metered consumption across all sites in the consolidation boundary, taken from supplier invoices. Estimated readings are flagged upstream and quarantined; an estimate never reaches this figure.

## Evidence the undertaking must hold

Documents of class `meter_reading_export`, `utility_invoice`; at least 12, covering the reporting period.

## Lawful omissions

None. This datapoint has no permitted omission.

Anything else that prevents disclosure is an internal failure, not an omission: the report does not issue, and the reason code says so on the face of the statement.
