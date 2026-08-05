# CLAUDE.md — Attestor

Multi-tenant regulated report factory.
AWS Bedrock AgentCore · Knowledge Bases · Guardrails · OpenSearch Serverless · Iceberg/Athena · Cedar · Terraform

> **Attestor** — *one who attests*. The auditor's word, applied to an AI system.
> Tagline: **every number carries its proof**.

---

## What this system does (mental model — keep this in mind every session)

An advisory firm serves several client companies. Each client must publish a **regulated
report** — a CSRD/ESRS sustainability statement, or an EU AI Act Annex IV technical
documentation — that an external auditor will inspect. Attestor produces those reports.

The whole design rests on one boundary:

| The LLM owns | Deterministic code owns |
|---|---|
| Interpreting the standard (*what does ESRS E1-6 ask of a logistics company?*) | **Every number** |
| Finding and citing evidence in an untrusted corpus | Every table, chart and unit conversion |
| Writing the narrative prose **around** a number | The decision to abstain |
| Diagnosing why a datapoint cannot be disclosed | Authorization, tenant scoping, document assembly |

**A figure that appears in a rendered document and cannot be traced to a datapoint contract
is a build failure.** That is the project, in one sentence.

### The three tenants

| Tenant | Vertical | What it proves |
|---|---|---|
| `helios` — Helios Logistics | CSRD / ESRS | The hard data engineering: heterogeneous evidence, units, consolidation boundary, restatement |
| `aegis` — Aegis Foods | CSRD / ESRS | **Isolation** — two peers in the same vertical is what makes the leakage suite meaningful |
| `lumen` — Lumen Advisory | EU AI Act (Annex IV) | **Generalization** — different corpus, different templates, *identical code path* |

`lumen`'s first engagement is Attestor itself: the platform produces its own Annex IV
technical documentation from its own repository as the evidence corpus.

---

## The five claims

Everything in this repository exists to make one of these five statements provable, in CI,
with no cloud and no credentials. If a change does not serve one of them, question it.

| # | Claim | Enforced by |
|---|---|---|
| **1** | **Indirect prompt injection does not execute.** The evidence corpus is untrusted user content. | `evals/injection/` + `src/attestor/security/` — block rate on a labelled poisoned corpus, **zero false positives** on benign |
| **2** | **A tenant never sees another tenant.** | `evals/isolation/` — 12 leakage paths (retrieval filter bypass, memory bleed, cache-key poisoning, Gateway tool-arg injection, session reuse, …) must all fail to leak |
| **3** | **No number comes from an LLM.** | `src/attestor/gates/provenance.py` — scans the **rendered** DOCX/XLSX/PPTX and fails on any numeral not registered to a datapoint id |
| **4** | **A report is reproducible.** Re-resolving as-of an earlier instant yields identical values and identical lineage hashes. | `src/attestor/datapoints/` + Iceberg snapshot pinning |
| **5** | **The system abstains, exactly and honestly.** On a corpus where evidence is deliberately missing for N datapoints, it must abstain **exactly N times, with 0 fabrications**. | `evals/abstention/` |

**Claim 5 is the cheapest and the most important.** A disciplined *"not disclosed — reason X"*
is a legal requirement of the CSRD, not a design nicety.

---

## The doctrine — what a responsible AI business does

These seven rules decide every "what happens when it goes wrong" question in this project.
When a new control is added, work out its answer to each of them before writing it.
Full reasoning in [ADR-0001](docs/adr/0001-fail-closed-with-a-recorded-key.md).

1. **The safe state is no output.** Every gate defaults to refusal.
2. **Every closed door has a key, and the key is a named human.** A control with no override
   does not prevent the override — it moves it outside the system, where it leaves no
   evidence. That is worse. The system may never open a door for itself: no model, no agent,
   no service principal may request, approve or classify one.
3. **An override changes what ships, never what is true.** It never relabels a defect as
   compliant. The reason code survives the override, in the record and on the page.
4. **An override is visible in the artefact, not only in a log.** It prints on the face of
   the statement, where the auditor reads. Our CloudWatch is not where an auditor looks.
5. **Overrides expire.** On expiry the finding returns and CI goes red again.
6. **Severity decides who turns the key** — how many approvers, in which roles, for how long.
7. **One door has no key at all.** `E_RESOLVER_ERROR`: a crashed resolver is an *unknown*
   deficiency, so nobody — including the approver — has the information the approval would
   be about. Having exactly one unopenable door is what keeps the other six honest; a
   break-glass that opens everything is a rubber stamp with extra ceremony.

---

## Non-negotiable engineering rules

**IaC only.** Every cloud resource in Terraform. No console deployments, ever. The one
exception is documented Day-1 manual work that has no API (Bedrock model access request,
the IdP application registration) — recorded in `docs/DAY-ONE.md`, never silently done.

**Bootstrap is local, everything else is CI.** `infra/bootstrap/` (remote state backend +
the OIDC role that CI assumes) is applied once from a laptop, because CI cannot create the
role it needs in order to run. Every other layer is applied **only** by a gated workflow.
A layer that can be applied from a laptop is a layer that will drift.

**No long-lived access keys. Ever.** SSO for humans, OIDC for CI, execution roles for
services, Secrets Manager for the rest. A static key in this repo is a failed build
(gitleaks gates every push).

**Terraform state is isolated per layer.** Cross-layer references are `outputs` →
`data` sources. Never a remote state read across layers.

**Deterministic first.** Before adding an LLM call, answer: *is there exactly one correct
answer here?* If yes, it is code. An LLM step with repair code underneath it is an
anti-pattern — delete the LLM step and keep the generator.

**Fail closed on safety and compliance; fail open on quality.** A guardrail error means no
output. A reranker timeout means unreranked results, logged.

**Every gate is attacked.** `make gate-proof` copies the repo, plants a *real* violation,
and fails unless the real gate refuses it **for the right reason**. Three rules keep it a
proof rather than a ritual: every gate must be **green first**; a non-zero exit is **not**
evidence (the *named* check must report the failure); a mutation whose target has moved is
reported **STALE**, not passed.

**Offline is the default.** The full test suite, every eval, every gate and every
`terraform validate` runs on a laptop with no AWS account. Cloud is for capturing proof,
not for validating logic.

**Done = runs + tested.** Generated-but-unrun code is not done.

---

## Repository layout

```
attestor/
├── contracts/            # THE SOURCE OF TRUTH — one YAML per regulated datapoint
│   ├── esrs/             #   ESRS/CSRD datapoints (tenants helios, aegis)
│   └── ai_act/           #   EU AI Act Annex IV datapoints (tenant lumen)
├── queries/              # The SQL each quantitative datapoint resolves through
├── templates/            # Document templates with typed placeholders
├── tenants/              # Tenant registry: identity, policy binding, corpus namespace
├── src/attestor/
│   ├── contracts/        # Contract schema, loader, cross-checks
│   ├── datapoints/       # Deterministic resolver + lineage + as-of resolution
│   ├── documents/        # Placeholder engine + DOCX/XLSX/PPTX renderers + render manifest
│   ├── gates/            # provenance · grounding · abstention · schema — the acceptance gates
│   ├── retrieval/        # Chunking strategies, KB config, retrieval eval harness
│   ├── agent/            # AgentCore tool handlers (MCP) + orchestration
│   ├── policy/           # Cedar policy authoring + offline evaluation
│   ├── security/         # Injection detection layers, isolation probes
│   └── observability/    # OTEL spans, per-tenant cost meter
├── evals/                # Labelled corpora + scored harnesses (credential-free)
├── infra/
│   ├── bootstrap/        # LOCAL apply only — state backend + CI OIDC role
│   ├── foundation/       # VPC (private), KMS, S3, Glue, budget guard, TTL reaper
│   ├── data/             # Iceberg tables, Athena workgroup
│   ├── knowledge/        # OpenSearch Serverless, Bedrock KBs, Guardrails
│   └── agent/            # AgentCore Runtime · Gateway · Identity · Memory · Observability
├── pipelines/            # Evidence ingestion + dbt-athena models
└── .github/workflows/    # CI (every PR) + gated bootstrap/deploy/destroy
```

---

## The contract layer — read this before touching anything

A **datapoint contract** (`contracts/**/*.yaml`) is the atomic unit of the system. It declares
what a regulated figure means, where it comes from, what tolerance it carries, what evidence
must exist for it, and **under which conditions the system must refuse to state it**.

Rules:

- A contract is **data, never code**. No Python imports a contract by name.
- A quantitative datapoint's `resolver` names a SQL file in `queries/`. That SQL is the only
  path from source data to a published figure.
- `abstention.reason_codes` are a closed vocabulary (`src/attestor/contracts/reason_codes.py`).
  A new reason code is a deliberate, reviewed addition — not a free-text string.
- Changing a contract's `unit`, `boundary` or `methodology` is a **restatement**: it requires a
  `supersedes` entry and CI will demand the prior-period note.
- Every contract is validated by `src/attestor/contracts/model.py` on every push. The model
  *is* the schema: a separate JSON Schema beside it would be a second description of one
  contract, and the two diverge on the first busy afternoon.

---

## Cost controls — always active

- **Nothing is applied outside a gated workflow.** No exceptions.
- Every resource carries `attestor:expires-at`. A scheduled reaper destroys what has expired.
- An AWS Budget action disables the deploy role at the configured threshold.
- OpenSearch Serverless is the dominant cost. It lives in **deliberate, bounded blocks** —
  stand up, run the retrieval bake-off, capture, destroy. It is never left standing.
- Per-tenant cost telemetry (`€/report`, `€/tenant`) is a first-class metric, not an afterthought.

---

## Git workflow

Conventional Commits: `<type>(<scope>): <description>`
Types: `feat | fix | infra | docs | refactor | test | chore`
Scopes: `contracts | datapoints | documents | gates | retrieval | agent | policy | security | infra | ci | evals`

One logical change per commit. Never commit `.env`, credentials, real account ids, or
tenant evidence that is not synthetic.

---

## Before any change — checklist

- Does this serve one of the five claims? Which one?
- Is there exactly one correct answer here? Then it is code, not a prompt.
- Can it be validated with no AWS account? If not, why not?
- If it is a gate: is there a `gate-proof` mutation that breaks it and proves it bites?
- If it touches a contract: does the change imply a restatement?
