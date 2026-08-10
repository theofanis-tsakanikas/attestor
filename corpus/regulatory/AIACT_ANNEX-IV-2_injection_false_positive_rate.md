# Annex IV §2(g) — False-positive rate of the input scanner on benign passages

- **standard**: EU_AI_ACT (Regulation (EU) 2024/1689, Annex IV)
- **datapoint**: `AIACT_ANNEX-IV-2_injection_false_positive_rate`
- **kind**: quantitative
- **reporting period basis**: point_in_time
- **unit**: ratio (published to 4 dp)

## What the clause requires

Proportion of benign passages that the retrieval scanner nonetheless withheld from the narrative turn, measured on the same labelled corpus as the block rate. A withheld benign passage degrades the disclosure the control was meant to protect.

## Methodology as declared

Passages labelled benign that the scanner withheld, over all passages so labelled. The benign set is adversarial by construction — correspondence quoting an instruction, a methodology note describing what the system must not do — because a false-positive rate measured against unrelated text is not a measurement of anything.

## Evidence the undertaking must hold

Documents of class `data_sheet`, `evaluation_report`; at least 2, not necessarily within the reporting period.

## Lawful omissions

None. This datapoint has no permitted omission.

Anything else that prevents disclosure is an internal failure, not an omission: the report does not issue, and the reason code says so on the face of the statement.
