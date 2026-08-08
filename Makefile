.DEFAULT_GOAL := help
SHELL := /bin/bash

# The venv when there is one, the ambient interpreter when there is not.
#
# `.venv/bin/pip` was hard-coded, which is true on a laptop and false on a CI runner, where
# the package is installed into the runner's own Python. `make package` therefore worked
# everywhere it was tried and failed in the one place it mattered — inside a deploy, four
# minutes in, on `No such file or directory`. A path that only exists on the machine that
# wrote it is the same defect as a git-ignored file in a build context.
VENV := .venv
PY   := $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
PIP  := $(if $(wildcard $(VENV)/bin/pip),$(VENV)/bin/pip,python3 -m pip)
RUFF := $(if $(wildcard $(VENV)/bin/ruff),$(VENV)/bin/ruff,ruff)
# Its own environment: checkov pins boto3==1.35.49 and the application needs a botocore
# that knows `bedrock-agentcore`. Created on demand by `iac-scan`.
CHECKOV_VENV := .venv-checkov
CHECKOV := $(if $(wildcard $(CHECKOV_VENV)/bin/checkov),$(CHECKOV_VENV)/bin/checkov,checkov)

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
	$(RUFF) check src tests scripts pipelines
	$(RUFF) format --check src tests scripts pipelines

.PHONY: fmt
fmt: ## Apply ruff formatting
	$(RUFF) format src tests scripts pipelines
	$(RUFF) check --fix src tests scripts pipelines

# ── The five claims ──────────────────────────────────────────────────────────

.PHONY: claims
claims: contracts-validate seed-check provenance abstain injection isolation reproducible ## Run every claim gate

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

.PHONY: seed-check
seed-check: ## The seeded lake must reproduce every recorded answer exactly
	$(PY) scripts/seed_recordings.py --check
	$(PY) pipelines/seed/generate.py --check

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

.PHONY: gateway-spec
gateway-spec: ## Regenerate the MCP tool schema terraform configures the gateway target with
	$(PY) -m attestor.cli.main gateway spec

.PHONY: regulatory-corpus
regulatory-corpus: ## Regenerate the regulatory corpus from the contract set
	$(PY) pipelines/ingest/regulatory.py

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

.PHONY: checkov-venv
checkov-venv:
	@test -x $(CHECKOV_VENV)/bin/checkov || { \
		echo "  creating $(CHECKOV_VENV) — checkov pins boto3==1.35.49 and cannot share ours"; \
		python3 -m venv $(CHECKOV_VENV) && $(CHECKOV_VENV)/bin/pip install -q --upgrade pip checkov; \
	}

.PHONY: iac-scan
iac-scan: checkov-venv ## checkov over the Terraform layers
	$(CHECKOV) -d infra --quiet --compact

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

.PHONY: preflight
preflight: ## Everything that must be true before the estate is stood up
	$(PY) scripts/preflight.py

.PHONY: preflight-fast
preflight-fast: ## The same, without gate-proof, terraform and checkov
	$(PY) scripts/preflight.py --fast

.PHONY: ci
ci: preflight ## Everything CI runs, in one command

