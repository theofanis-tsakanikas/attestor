---
document_id: SYSDOC-ATT-2026
document_class: system_documentation
tenant: lumen
covers_from: 2026-01-01
covers_to: 2026-12-31
---

# Technical documentation — system description

## General description and intended purpose

Attestor is a multi-tenant reporting system that produces CSRD/ESRS sustainability statements
and EU AI Act Annex IV technical documentation. Its intended purpose is to draft regulated
narrative and assemble regulated documents from figures resolved deterministically from an
undertaking's own records. It does not provide advice and it does not decide compliance.

## Architecture

Three planes. A data plane holds the undertaking's records as versioned tables and resolves
every quantitative datapoint through committed SQL. A reasoning plane drafts narrative from
retrieved evidence and from figures it receives read-only. A document plane renders the
statement and refuses to publish any numeral not registered to a datapoint identifier.

Tenants are separated at the identity boundary: one identity provider per tenant, one gateway
per tenant, and retrieval filtered by tenant at the index rather than after it.

## Human oversight by design

The system is built so that a person can intervene at the point where it would otherwise
proceed. Every refusal carries a machine-readable reason code and a human-readable sentence,
and no refusal can be lifted by the system itself.

## Data governance

Evidence is immutable once ingested and addressed by content hash; a document that changes is
a new document. Records that fail their data contract are quarantined with the rule they broke
rather than dropped, and a figure computed over an incomplete set is refused rather than
published with a footnote.

## Record keeping

Each run records which snapshot of each table it read, the hash of every query it executed and
the identifier of every evidence passage it cited. That record is what allows a figure to be
re-derived after the fact.
