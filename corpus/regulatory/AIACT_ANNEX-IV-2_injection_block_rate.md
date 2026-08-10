# Annex IV §2(g) — Block rate against manipulated input passages

- **standard**: EU_AI_ACT (Regulation (EU) 2024/1689, Annex IV)
- **datapoint**: `AIACT_ANNEX-IV-2_injection_block_rate`
- **kind**: quantitative
- **reporting period basis**: point_in_time
- **unit**: ratio (published to 4 dp)

## What the clause requires

Proportion of passages carrying a known prompt-injection attempt that the retrieval scanner withheld from the narrative turn, measured on the labelled corpus described in the data sheet. Reported together with the false-positive rate on benign passages, which is the only way the figure can be read.

## Methodology as declared

Passages labelled as carrying an injection attempt that the scanner withheld, over all passages so labelled. The denominator is the labelled set, not the set the scanner flagged: the flagged-over-flagged form is precision, and it improves when a detector becomes shy. Art. 15(5) requires resilience to inputs designed to cause the system to make a mistake; the corpus is the input surface, so that is where it is measured.

## Evidence the undertaking must hold

Documents of class `data_sheet`, `evaluation_report`; at least 2, not necessarily within the reporting period.

## Lawful omissions

None. This datapoint has no permitted omission.

Anything else that prevents disclosure is an internal failure, not an omission: the report does not issue, and the reason code says so on the face of the statement.
