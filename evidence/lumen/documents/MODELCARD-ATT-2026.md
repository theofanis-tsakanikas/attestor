---
document_id: MODELCARD-ATT-2026
document_class: model_card
tenant: lumen
---

# Model card — Attestor regulated reporting assistant

## Intended purpose

Attestor produces regulated sustainability and conformity reports for advisory clients. Its
intended purpose is to draft the narrative around a figure and to cite the evidence behind it.
It is not intended to compute, estimate, adjust or reconcile any disclosed quantity, and it is
not intended to decide whether a disclosure may be omitted.

Intended users are qualified reporting preparers at an advisory firm, working on behalf of an
undertaking subject to the Corporate Sustainability Reporting Directive or to the AI Act. The
system is not intended for use by an undertaking's own staff without a preparer, and it is not
a consumer product.

## What the system decides and what it does not

Every quantitative figure is resolved deterministically against a versioned store of the
undertaking's records. The language model receives already-resolved figures as read-only
context and may not place one in its output; where a figure belongs, it emits a placeholder
that a resolver fills. A numeral in generated prose that is not registered to a datapoint is
treated as a build failure rather than as a formatting problem.

The decision to abstain from a disclosure is likewise deterministic. It is taken from a closed
vocabulary of reason codes and never from model output.

## Reasonably foreseeable misuse

Two are anticipated. The first is using a drafted narrative without preparer review; the
system therefore refuses to issue any report in which a datapoint is unresolved or outside its
tolerance. The second is treating retrieved evidence as instruction: the evidence corpus is
untrusted content, and every passage is enveloped and screened before it reaches a prompt.

## Performance and its limits

Accuracy is reported against a held-out evaluation set and disclosed with its confusion matrix
in the technical documentation. No accuracy figure is produced by the model itself.

The system does not verify that submitted evidence is authentic. It records what it was given,
what it read and what it could not read, and refuses rather than interpolating where evidence
for a datapoint is absent.
