---
document_id: OVERSIGHT-ATT-2026
document_class: oversight_procedure
tenant: lumen
---

# Human oversight procedure

## Purpose

This procedure describes the measures by which natural persons oversee the system in
operation, as required by the AI Act's provisions on human oversight.

## Roles

Three roles, held by different people.

The **preparer** reviews every drafted narrative against the evidence it cites and either
accepts it or returns it. A report cannot be issued without a preparer's acceptance.

The **approver** is the only role that may grant an override when a control has refused. An
override names the approver, states the reason code it applies to, and expires on a stated
date.

The **assurance reviewer** reads the omissions register and the override register before the
statement is signed.

## What oversight can do while the system runs

The preparer can stop a run, reject a drafted paragraph, and require a datapoint to be
resolved again. Refusals are surfaced with the reason and the datapoint they concern rather
than as a generic failure, so the person deciding has the information the decision is about.

## What oversight cannot do

No role may relabel a defect as compliant. An override changes what ships and never what is
true: the reason code survives it, in the record and on the face of the statement, and the
finding returns when the override expires.

No override exists for a resolver that has crashed. Where the system does not know why it
could not produce a figure, nobody — including the approver — has the information an approval
would be about, so that door stays shut. Having one door that cannot be opened is what keeps
the others honest.

## Automation bias

Preparers are instructed that a drafted narrative is a proposal and that its fluency is not
evidence of its correctness. Every assertion carries the identifier of the passage it came
from, so review is a comparison against a source rather than an impression of plausibility.
