# ADR-0003 — OpenSearch Serverless, and paying for it

**Status:** accepted · **Date:** 2026-08-05

## Context

OpenSearch Serverless is the largest single line item in this estate. Cheaper vector stores
exist, and for a system answering a thousand queries a month one of them would be the right
call — the earlier draft of this project chose one, on exactly that reasoning.

That reasoning was wrong here, and the way it was wrong is worth recording: it optimised
idle cost in a system that is never idle for long, and paid for it with a capability that is
load-bearing.

## Decision

OpenSearch Serverless, for two things the cheaper options do not do:

**Hybrid search.** A user asks about "E1-6 §44(a)". Dense retrieval is poor at article
numbers — they carry almost no semantic signal — and BM25 is excellent at them. Losing the
lexical half means losing the queries a compliance user actually types.

**Metadata filtering evaluated at the index.** The tenant filter has to be a query
constraint, not a post-filter applied to rows that were already read. The difference is
invisible in a demo and is the entire difference between a filter and a redaction.

## Consequences

- The estate is ephemeral, and that is the answer to idle cost — not a weaker store. Stand
  up, run the bake-off, capture, destroy. Every resource carries `attestor:expires-at` and a
  reaper reports what has overstayed.
- `standby_replicas` is off during build blocks and on for the capture run, so the screenshots
  show the topology anyone would actually deploy rather than the one that was cheapest that
  week.
- The bake-off measures chunking offline and reports the embedding-model comparison as
  pending, because that one genuinely needs the estate. An admitted gap gets filled; an
  invented number gets quoted.
