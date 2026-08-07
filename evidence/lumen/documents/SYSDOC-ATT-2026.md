---
document_id: SYSDOC-ATT-2026
document_class: system_documentation
tenant: lumen
---

# Technical documentation — general description of the system

## Intended purpose and users

Attestor is a multi-tenant reporting system that produces sustainability statements and
technical documentation for regulated undertakings. Its intended purpose is to draft regulated
narrative and to assemble regulated documents from figures resolved deterministically from an
undertaking's own records. It does not provide advice and it does not determine compliance.

## Architecture

The system has three planes. A data plane holds the undertaking's records as versioned tables
and resolves every quantitative datapoint through committed queries. A reasoning plane drafts
narrative from retrieved evidence and from figures it receives read-only. A document plane
renders the statement and refuses to publish any numeral not registered to a datapoint
identifier.

Tenants are separated at the identity boundary. Each has its own identity provider and its own
gateway, and retrieval is filtered at the index rather than after the results are read, so a
query on behalf of one undertaking cannot reach another's corpus.

## Human oversight by design

The system is built so that a person can intervene where it would otherwise proceed. Every
refusal carries a machine-readable reason code and a sentence a reader can act on, and no
refusal can be lifted by the system itself.

Oversight is designed in rather than added on. The system never issues a report on its own
authority: a preparer accepts each drafted narrative against the evidence it cites, and a
report with an unresolved or out-of-tolerance datapoint cannot be issued at all, so the
decision to proceed always rests with a person. Where a control refuses, the refusal names the
datapoint and the reason rather than failing generically, which is what makes intervention
possible rather than merely permitted.

The interface presents a drafted paragraph as a proposal beside the passages it rests on, so
that review is a comparison against sources rather than a judgement of plausibility. This is
the system's answer to automation bias, and it is a property of what the operator is shown
rather than of what the model produces.

The measures a deployer must put in place around the system — who holds each role, how
acceptance is recorded, how an override is approved and when it expires — are set out in the
human oversight procedure that accompanies this documentation.

## Data governance

Evidence is immutable once ingested and addressed by the hash of its content, so a document
that changes is a different document. Records that fail their data contract are quarantined
with the rule they broke rather than dropped, and a figure computed over an incomplete set is
refused rather than published with a footnote.

## Record keeping

Each run records which version of each table it read, the hash of every query it executed and
the identifier of every evidence passage it cited. That record is what allows a figure to be
re-derived after the fact by someone who was not present when it was produced.
