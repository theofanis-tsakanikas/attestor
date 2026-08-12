# Changelog

Notable changes to Attestor. Format loosely follows [Keep a Changelog][kac]; the project is not
versioned or released, so entries are grouped by the date the work landed on `main`.

[kac]: https://keepachangelog.com/en/1.1.0/

---

## Unreleased

Deferred, in the order it is likely to be picked up.

- **Close the declared-versus-present evidence gap.** `helios` declares 28 evidence documents and
  ships 5; `aegis` declares 26 and ships none. The evidence check reads the manifest by design, so
  nothing currently fails. It should — as a warning first, then a gate.
- **Give `aegis` a corpus.** Its refusal is currently part data-driven and part structural. With
  documents on disk, its blockers become purely about the data, which is the point of the tenant.
- **Run the retrieval bake-off against live embedding models.** `evals/retrieval` scores chunking
  strategies offline; the embedding-model choice is recorded as pending live capture.
- **Move the placeholder list into the system turn.** The guardrail intermittently blocks the
  evidence turn when the placeholder list travels with it.
- **Residency and multi-region.** Everything is `eu-central-1` with no documented story for a split
  deployment.

---

## 2026-08-12 — deployed, verified, destroyed

The estate was deployed from CI, verified against the live account, recorded, and torn down the
same day. Everything below came out of making that round trip actually work.

### Added
- `scripts/verify_live_estate.py` (32 checks) and `scripts/verify_agentcore.py` (10 checks) — the
  five claims asserted against a deployed estate rather than fixtures.
- `scripts/check_workflow_permissions.py` — a reusable workflow cannot be granted more than its
  caller. Catches a `startup_failure` that produces no logs at all.
- `scripts/check_session_duration.py` — a workflow may not ask for a longer session than the role's
  `max_session_duration`. Two files, each correct alone, disagreeing.
- Lake Formation grants derived per table from the dbt gold models, replacing a `TableWildcard`
  grant that AWS refuses to the resource creator.
- `.gitleaks.toml` with a custom rule for 12-digit account identifiers.
- `LICENSE` (MIT), `DISCLAIMER.md`, `SECURITY.md`, `CONTRIBUTING.md`, `.env.example`, and this file.
- Branch protection on `main`: six required checks, no direct pushes.

### Fixed
- The Athena partition-repair list was maintained by hand and had fallen behind the schema; it now
  asks Glue which tables are partitioned by `tenant_id`.
- The destroy lock was released after the step that needed it, not before.
- The post-destroy survivor check asked the account rather than this project's estate, so it
  reported other projects' resources as survivors.
- Cedar `CreatePolicy` validates action names against the gateway, so the gateway target must reach
  `READY` before policies are written; the attach step now polls instead of assuming.
- `role-duration-seconds: 10800` against a role capped at 3600 — STS refused the assume before the
  job touched a resource.
- Subnet teardown now waits a bounded 300s and exits with the actual wait time. AgentCore's
  `ela-attach` interfaces are owned by `amazon-aws` and cannot be detached or deleted by the
  account; code that tried to reclaim them was removed, and the constraint recorded.
- The cost meter now stops before the part of teardown that may not finish.

### Changed
- Contract constraints (`max_words`, `min_citations`, the digit rule) are stated in the system turn
  rather than the evidence turn, with refusal feedback fed back on retry.
- Two figures in `lumen`'s Annex IV — injection block rate and false-positive rate — are now
  **measured** by running this repository's own scanner over its own labelled corpus, rather than
  seeded to a target. Provenance for those rows reads `synthetic+measured`.

---

## 2026-08-05 to 2026-08-11 — the system

Built in dependency order, each layer green offline before the next was started.

### Added
- **Contract layer** — 18 datapoint contracts as YAML, unit algebra, safe derivation, closed
  reason-code vocabulary, restatement rules, and the break-glass override register: fail closed,
  with a key held by a named human, expiring, printed on the artefact.
- **Deterministic resolver** — 14 SQL resolvers, lineage records, and as-of resolution pinned to
  Iceberg snapshot ids.
- **Document layer** — typed placeholders, DOCX/XLSX/PPTX renderers, render manifests, and the
  assurance annex printed inside the artefact an auditor receives.
- **Gates** — provenance, grounding, abstention and schema, each scanning the rendered binary.
- **`make gate-proof`** — 24 planted violations, each a real one-line diff, each required to be
  refused by a *named* check. Green-first, exit-code-is-not-evidence, and STALE-not-passed.
- **Security** — layered injection detection, a labelled poisoned corpus, and 12 isolation probes.
- **Agent surface** — AgentCore Gateway and Runtime per tenant, MCP tool handlers, Cedar policies
  enforced at the edge, with `request_override` forbidden to every agent.
- **Infrastructure** — five Terraform layers; `bootstrap` applied once from a laptop because CI
  cannot create the role it needs, everything else only by a gated workflow.
- **Lakehouse** — Iceberg tables on S3, 13 dbt gold models, an Athena workgroup, and a deterministic
  seed generator that builds rows backwards from recorded targets.
- **Observability** — OTEL spans and a per-tenant cost meter reporting €/report and €/tenant,
  including for runs that end in a refusal.
- **Three ADRs** — fail closed with a recorded key; templates as YAML; OpenSearch Serverless over
  cheaper stores.
