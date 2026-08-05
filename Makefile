.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := .venv/bin/python
PIP := .venv/bin/pip

# ─────────────────────────────────────────────────────────────────────────────
# Everything above the "cloud" section runs with NO AWS account and NO
# credentials. That is the point: no claim in this repository needs a cloud to
# be checked.
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

.venv:
	python3.12 -m venv .venv
	$(PIP) install --upgrade pip

.PHONY: install
install: .venv ## Create the venv and install the package with dev extras
	$(PIP) install -e ".[dev]"

.PHONY: test
test: ## Full test suite — offline, no credentials
	$(PY) -m pytest

.PHONY: lint
lint: ## ruff check + format check (the exact command CI runs)
	.venv/bin/ruff check src tests evals scripts
	.venv/bin/ruff format --check src tests evals scripts

.PHONY: fmt
fmt: ## Apply ruff formatting
	.venv/bin/ruff format src tests evals scripts
	.venv/bin/ruff check --fix src tests evals scripts

# ── The five claims ──────────────────────────────────────────────────────────

.PHONY: claims
claims: contracts-validate provenance abstain injection isolation reproducible ## Run every claim gate

.PHONY: contracts-validate
contracts-validate: ## Validate every datapoint contract against the schema + cross-checks
	$(PY) -m attestor.cli.main contracts validate

.PHONY: provenance
provenance: ## CLAIM 3 — no numeral in a rendered document without a datapoint id
	$(PY) -m attestor.cli.main gate provenance --all

.PHONY: abstain
abstain: ## CLAIM 5 — exactly N abstentions, 0 fabrications
	$(PY) -m attestor.cli.main eval abstention

.PHONY: injection
injection: ## CLAIM 1 — indirect prompt injection block rate + 0 false positives
	$(PY) -m attestor.cli.main eval injection

.PHONY: isolation
isolation: ## CLAIM 2 — 12 cross-tenant leakage paths, all closed
	$(PY) -m attestor.cli.main eval isolation

.PHONY: reproducible
reproducible: ## CLAIM 4 — as-of resolution is value- and lineage-identical
	$(PY) -m attestor.cli.main eval reproducibility

.PHONY: gate-proof
gate-proof: ## Break every gate on purpose; each must be refused, for the right reason
	$(PY) scripts/gate_proof.py

# ── Retrieval ────────────────────────────────────────────────────────────────

.PHONY: retrieval-eval
retrieval-eval: ## Score the golden retrieval set against the recorded index snapshots
	$(PY) -m attestor.cli.main eval retrieval

.PHONY: bake-off
bake-off: ## Compare chunking strategies + embedding models (offline replay of a live run)
	$(PY) -m attestor.cli.main retrieval bake-off --replay

# ── Documents ────────────────────────────────────────────────────────────────

.PHONY: run
run: ## Resolve, render, gate and record one tenant (TENANT=helios)
	$(PY) -m attestor.cli.main run --tenant $(TENANT)

.PHONY: run-all
run-all: ## Every tenant, then the dashboard. A blocked tenant still records why.
	@for t in helios aegis lumen; do $(PY) -m attestor.cli.main run --tenant $$t || true; done
	$(PY) -m attestor.cli.main dashboard
	@echo "open out/dashboard.html"

.PHONY: dashboard
dashboard: ## Build the static page from the recorded runs
	$(PY) -m attestor.cli.main dashboard

.PHONY: report
report: ## Render a tenant's full report set (DOCX + XLSX + PPTX) into out/
	$(PY) -m attestor.cli.main report render --tenant $(TENANT)

.PHONY: ingest-plan
ingest-plan: ## Validate the evidence manifests and report what would be uploaded
	$(PY) pipelines/ingest/evidence.py

.PHONY: govern-docs
govern-docs: ## Regenerate the governance docs from code (CI runs this with --check)
	$(PY) -m attestor.cli.main govern generate

# ── Policy ───────────────────────────────────────────────────────────────────

.PHONY: policy
policy: ## Validate Cedar policies + run the policy attack set (must all be DENY)
	$(PY) -m attestor.cli.main policy verify

# ── Infrastructure (offline validation only — no cloud calls) ────────────────

.PHONY: tf-fmt
tf-fmt: ## terraform fmt across every layer
	terraform fmt -recursive infra

.PHONY: tf-validate
tf-validate: ## terraform validate per layer, offline (no backend, no provider creds)
	@for layer in infra/*/; do \
		echo "── $$layer"; \
		terraform -chdir=$$layer init -backend=false -input=false >/dev/null || exit 1; \
		terraform -chdir=$$layer validate || exit 1; \
	done

.PHONY: iac-scan
iac-scan: ## checkov over the Terraform layers
	.venv/bin/checkov -d infra --quiet --compact

.PHONY: package
package: ## Vendor the library into the Lambda source dir so terraform can zip it
	rm -rf infra/agent/tools/attestor infra/agent/tools/*.dist-info
	$(PIP) install --quiet --target infra/agent/tools --no-compile .
	cp -R contracts queries prompts templates tenants overrides policy evidence \
		infra/agent/tools/
	@echo "packaged infra/agent/tools ($$(du -sh infra/agent/tools | cut -f1))"

# ── Cloud (never run implicitly; always a deliberate act) ────────────────────

.PHONY: cost
cost: ## Print the current estate's running cost and time-to-expiry
	$(PY) -m attestor.cli.main cost status

.PHONY: ci
ci: lint test claims policy tf-validate ## Everything CI runs, in CI's order

