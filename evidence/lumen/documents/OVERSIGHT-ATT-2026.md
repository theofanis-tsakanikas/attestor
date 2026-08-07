---
document_id: OVERSIGHT-ATT-2026
document_class: oversight_procedure
tenant: lumen
covers_from: 2026-01-01
covers_to: 2026-12-31
---

# Human oversight procedure

## Purpose

This procedure describes the measures by which natural persons oversee Attestor in operation,
as required by Article 14 of the EU AI Act.

## Oversight roles

Three named roles, held by different people:

- **Preparer** — reviews every drafted narrative against the evidence cited, and either accepts
  it or returns it. A report cannot be issued without a preparer's acceptance.
- **Approver** — the only role that may grant an override when a control has refused. An
  override names the approver, states the reason code it applies to, and expires on a stated
  date.
- **Assurance reviewer** — reads the omissions register and the override register before the
  statement is signed.

## What oversight can do at run time

The preparer can stop a run, reject a drafted paragraph, and require a datapoint to be
re-resolved. Refusals are surfaced with the reason and the datapoint they concern rather than
as a generic failure, so the person deciding has the information the decision needs.

## What oversight cannot do

No role may relabel a defect as compliant. An override changes what ships and never what is
true: the reason code survives it, in the record and on the face of the statement, and the
finding returns when the override expires.

No override exists for a crashed resolver. Where the system does not know why it could not
produce a figure, nobody — including the approver — has the information an approval would be
about, so the door stays shut.

## Automation bias

Preparers are instructed that a drafted narrative is a proposal and that its fluency is not
evidence of its correctness. Every assertion in a draft carries the identifier of the passage
it came from, so review is a comparison rather than an impression.
