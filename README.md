# Attestor

**A multi-tenant regulated report factory. Every number carries its proof.**

*AWS Bedrock AgentCore · Knowledge Bases · Guardrails · OpenSearch Serverless · Iceberg / Athena · Cedar · Terraform*

> **Attestor** — *one who attests*. The auditor's word, applied to an AI system.

---

> **Status: in construction.** The code, contracts, gates and evals in this repository run
> offline today. Nothing has been deployed yet — the sections marked *pending live capture*
> will carry screenshots from a real, gated run that is then destroyed to zero cost. Claims
> in this README are only made about what is actually here; there is a
> [scoreboard](#the-scoreboard) rather than adjectives.

---

## The problem

An advisory firm produces regulated reports for its clients: a CSRD sustainability statement
under ESRS, an EU AI Act Annex IV technical file. These documents are read by an external
auditor whose job is to disbelieve them. Every figure has to trace back to a source record;
every claim has to trace back to evidence; a figure that cannot be supported has to be
*declared missing* rather than estimated into existence.

Generative AI is genuinely useful here — interpreting a 1,100-datapoint standard, finding the
one supplier attestation that supports a claim, drafting the narrative around a number. It is
also the fastest way yet invented to produce a plausible figure that is not true.

So the whole system is built around one boundary:

| The LLM owns | Deterministic code owns |
|---|---|
| Interpreting the standard | **Every number** |
| Finding and citing evidence in an untrusted corpus | Every table, chart and unit conversion |
| Writing the narrative **around** a number | The decision to abstain |
| Diagnosing why a datapoint cannot be disclosed | Authorization, tenant scoping, document assembly |

**A figure that appears in a rendered document and cannot be traced to a datapoint contract
is a build failure.**

---

## The five claims

Each one is checked in CI, on a laptop, with no AWS account and no credentials.

| # | Claim | Where it is proved |
|---|---|---|
| **1** | **Indirect prompt injection does not execute.** The evidence corpus is untrusted user content. | `evals/injection/` — block rate on a labelled poisoned corpus, **zero false positives** on benign documents |
| **2** | **A tenant never sees another tenant.** | `evals/isolation/` — 12 leakage paths, all closed |
| **3** | **No number comes from an LLM.** | `src/attestor/gates/provenance.py` — scans the **rendered** DOCX/XLSX/PPTX and fails on any numeral not registered to a datapoint |
| **4** | **A report is reproducible.** | As-of resolution against a pinned Iceberg snapshot: identical values, identical lineage hashes |
| **5** | **The system abstains, exactly and honestly.** | `evals/abstention/` — exactly N abstentions on a corpus with N deliberate evidence gaps, **0 fabrications** |

### The scoreboard

Produced by `make claims` on a laptop with no AWS account. Every figure below is the output
of a command in this repository, not a summary of one.

| check | result |
|---|---|
| **claim 1** · indirect prompt injection | **15/15** poisoned documents flagged, each for the rule it was written for · **0/10** benign wrongly flagged |
| **claim 2** · tenant isolation | **12/12** routes closed |
| **claim 3** · no number from an LLM | **4/4** artefacts clean across two regulatory regimes · 477 numerals checked |
| **claim 4** · reproducibility | **9/9** (ESRS) and **7/7** (AI Act) lineage ids identical across runs |
| **claim 5** · disciplined abstention | **24/24** expected refusals · **0** fabrications · nothing undamaged refused |
| `make gate-proof` | **10 refused, 0 accepted, 0 stale** |
| test suite | **258 passing**, offline, credential-free |

The last two rows are the ones worth reading first. A suite tells you the code does what it
does; `gate-proof` breaks each control on purpose and requires the *named* gate to refuse it,
for the right reason — because a gate that has never been shown to fail is a comment.

---

## The idea that carries the most weight

A contract cannot pre-authorize its own failure.

The reasons a figure may go undisclosed are a closed vocabulary
([`reason_codes.py`](src/attestor/contracts/reason_codes.py)) split in two:

- **Lawful omissions** — the standard permits them. *Not material. Phase-in. Seriously
  prejudicial.* They are printed in the report, entered in the omissions register, and an
  auditor accepts them as an answer.
- **Internal failures** — a resolver crashed, source rows were quarantined, the evidence is
  out of period. These are not answers. **They block the report.**

A datapoint contract may declare which *lawful* omissions apply to it. It cannot declare an
internal failure, because that is how "we could not compute it" quietly becomes "it was not
material" — and that laundering is the exact failure this system exists to make impossible.

---

## The contract layer

One YAML file per regulated figure, under [`contracts/`](contracts/). It is the source of
truth: what the figure means, which clause demands it, how it is computed, what evidence must
exist behind it, and what tolerance it carries.

```yaml
id: ESRS_E1-6_gross_scope_2_location
kind: quantitative
unit: tCO2e
precision: 0
boundary: operational_control
resolver:
  kind: derived
  expression: "{ESRS_E1-5_electricity_consumption} * {ESRS_E1_grid_factor_GR}"
```

Three things happen to that expression before it is allowed to load:

1. It is parsed with `ast` under a four-operator whitelist — no calls, no attribute access,
   no `eval`. A published number never sits behind arbitrary code.
2. Its **dimension** is inferred and checked against the declared unit. `MWh × tCO2e/MWh`
   produces `tCO2e`; if it produced anything else the contract set refuses to load. This is
   the cheapest real bug the repository prevents — multiply megawatt-hours by a factor quoted
   in `gCO2e/kWh` without converting and you publish a number a thousand times too small,
   and prose cannot check arithmetic.
3. Its operands are resolved against the rest of the set: they must exist, they must not be
   model-authored, and the derivation graph must be acyclic.

Every emission factor is a contract too — with a citation, an approver and a date, because a
magic number in a regulated report should have a human's name on it.

---

## Repository layout

| Path | Purpose |
|---|---|
| [`contracts/`](contracts/) | **The source of truth** — one YAML per regulated datapoint |
| [`queries/`](queries/) | The SQL each quantitative datapoint resolves through, parameters bound, never interpolated |
| [`prompts/`](prompts/) | Versioned prompts. Never inline in code |
| [`templates/`](templates/) | Document templates with typed placeholders |
| [`tenants/`](tenants/) | Tenant registry — identity, policy binding, corpus namespace |
| [`src/attestor/contracts/`](src/attestor/contracts/) | Contract model, unit algebra, safe derivation, loader |
| [`src/attestor/datapoints/`](src/attestor/datapoints/) | Deterministic resolver, lineage, as-of resolution |
| [`src/attestor/documents/`](src/attestor/documents/) | Placeholder engine, DOCX/XLSX/PPTX renderers, render manifest |
| [`src/attestor/gates/`](src/attestor/gates/) | The acceptance gates |
| [`src/attestor/retrieval/`](src/attestor/retrieval/) | Chunking strategies, KB config, retrieval eval harness |
| [`src/attestor/agent/`](src/attestor/agent/) | AgentCore tool handlers and orchestration |
| [`src/attestor/policy/`](src/attestor/policy/) | Cedar policy authoring and offline evaluation |
| [`src/attestor/security/`](src/attestor/security/) | Injection detection, isolation probes |
| [`evals/`](evals/) | Labelled corpora and scored harnesses, credential-free |
| [`infra/`](infra/) | Terraform. `bootstrap/` applies from a laptop; every other layer only from a gated workflow |

---

## Running it

```bash
make install          # venv + editable install
make run-all          # resolve, render, gate and record every tenant, then build the page
open out/dashboard.html

make test             # full suite, offline
make claims           # the five claim gates
make gate-proof       # break every gate on purpose; each must be refused, for the right reason
```

Requires Python 3.12+. No AWS account, no credentials, no network.

## Where the results are

Three surfaces, all generated from one run record so they cannot disagree.

**The artefacts** — `out/<tenant>/`: the Word statement, the Excel annex, the board deck, each
beside the render manifest the provenance gate checked it against.

**The dashboard** — `out/dashboard.html`. One self-contained file, no scripts, no network. It
answers the two questions somebody deciding whether to file actually has: *can we issue?* and
*what are we admitting to?* Blockers are red and first; accepted defects show who signed and
when the acceptance lapses. It still opens after the estate is destroyed, which is when
somebody usually wants it.

**The warehouse** — `gold.report_run` and `gold.report_datapoint`, with views in
[`analytics/views.sql`](analytics/views.sql). That is where the questions a single run cannot
answer live: which reason code blocks every quarter, how much of a tenant's disclosure rate
rests on overrides that are about to lapse, and what a blocked run cost to produce nothing.

---

## The three tenants

| Tenant | Vertical | What it proves |
|---|---|---|
| `helios` — Helios Logistics | CSRD / ESRS | Heterogeneous evidence, units, consolidation boundary, restatement |
| `aegis` — Aegis Foods | CSRD / ESRS | **Isolation** — two peers in one vertical is what makes the leakage suite mean something |
| `lumen` — Lumen Advisory | EU AI Act (Annex IV) | **Generalization** — different corpus, different templates, identical code path |

`lumen` issues. Its Annex IV technical file renders from seven datapoints — a narrative
intended-purpose section, an accuracy figure recomputed from the evaluation run and
cross-checked against the confusion matrix, a derived error rate, a residual-risk count, a
serious-incident count — through the **same code path** the CSRD statement uses. Different
standard, different clause numbering, different evidence classes, different identity
provider; no branch in `src/`, and
[a test that fails if one appears](tests/contracts/test_verticals.py).

The system it documents is Attestor itself. That is not a joke about recursion: a platform
that produces conformity documentation for other people's AI systems and cannot produce its
own is making a claim it has not tested. The evidence corpus is currently synthetic
stand-ins for repository artefacts that exist — the evaluation report corresponds to
`evals/`, the oversight procedure to ADR-0001 and the override register — and replacing them
with generated exports is the next increment rather than something pretended to have
happened.

---

## Cost posture

The estate is ephemeral by construction. Nothing is applied outside a gated workflow, every
resource carries an `attestor:expires-at` tag that a scheduled reaper enforces, and an AWS
Budget action disables the deploy role at its threshold. OpenSearch Serverless — the dominant
cost — lives in deliberate, bounded blocks: stand up, run the retrieval bake-off, capture,
destroy.

Per-tenant cost telemetry (`€/report`, `€/tenant`) is a first-class metric.

---

## Licence

MIT — see [LICENSE](LICENSE). Engineering rules live in [CLAUDE.md](CLAUDE.md).
