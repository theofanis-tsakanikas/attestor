# Contributing

Attestor is a portfolio project, so pull requests from strangers are unlikely and welcome in equal
measure. What follows is what the repository expects of any change, including mine.

## Setup

Python 3.12+ and `make`. No AWS account, no credentials, no Docker.

```bash
git clone https://github.com/theofanis-tsakanikas/attestor.git
cd attestor
make install          # venv + editable install with the dev extra
make ci               # everything CI runs — about a minute
```

`make ci` green on a laptop with no cloud access is the contract. If a change cannot be validated
offline, that is worth a paragraph in the pull request explaining why.

## Dependencies

Runtime dependencies live in `pyproject.toml` under `dependencies` and are deliberately few. Two
rules, both enforced:

- **A declared dependency that nothing imports is a failing test.** `tests/test_dependencies.py`
  checks it. A package that ships in the Lambda payload and the container for no reason is not free.
- **Cloud clients are an optional extra (`.[cloud]`), never a hard dependency.** The full test
  suite, every eval and every gate must run without `boto3` installed.

Version floors carry a comment saying what breaks below them. `boto3>=1.40` is not cosmetic — the
`bedrock-agentcore` data plane does not exist before it, and `boto3.client()` on an unknown service
fails at call time rather than at import.

## What a change is expected to answer

The same five questions every change in this repository answers, from [`CLAUDE.md`](CLAUDE.md):

1. **Which of the five claims does this serve?** If none, say why it belongs.
2. **Is there exactly one correct answer here?** Then it is code, not a prompt. An LLM step with
   repair code underneath it is an anti-pattern — delete the step and keep the generator.
3. **Can it be validated with no AWS account?** If not, why not.
4. **If it is a gate: is there a `gate-proof` mutation that breaks it?** A gate nobody has attacked
   is a gate nobody knows works. Add the mutation in the same pull request.
5. **If it touches a contract: does the change imply a restatement?** Changing a `unit`, `boundary`
   or `methodology` needs a `supersedes` entry and a prior-period note, and CI will demand both.

## Things that will be rejected

- **A new reason code as free text.** `abstention.reason_codes` is a closed vocabulary in
  `src/attestor/contracts/reason_codes.py`. Adding one is a deliberate, reviewed change.
- **A cloud resource created outside Terraform.** Every resource is IaC. The only exception is
  Day-1 manual work that has no API, recorded in [`docs/DAY-ONE.md`](docs/DAY-ONE.md) — never done
  silently.
- **A control with no override, or an override the system can grant itself.** Both are argued in
  [ADR-0001](docs/adr/0001-fail-closed-with-a-recorded-key.md). `E_RESOLVER_ERROR` is the one door
  with no key, and it stays that way.
- **Anything that lets a model produce a figure**, however well-validated afterwards.
- **A remote state read across Terraform layers.** Cross-layer references are `outputs` → `data`.
- **A static credential or a real account identifier.** `gitleaks` gates every push.

## Commits and pull requests

Conventional Commits, one logical change per commit:

```
<type>(<scope>): <description>

type:  feat | fix | infra | docs | refactor | test | chore
scope: contracts | datapoints | documents | gates | retrieval | agent
       policy | security | infra | ci | evals
```

`main` is protected. Six checks must pass before a merge: secret scan, lint and tests, the five
claims, attack our own gates, override register, and terraform + checkov. Never commit `.env`,
credentials, real account identifiers, or tenant evidence that is not synthetic.

## Style

`ruff` decides formatting and lint; `make fmt` applies it and `make lint` checks it. Beyond that,
one convention that is unusual enough to state: **comments in this repository explain why a line is
the way it is, especially when it looks wrong.** A version floor, an absent dependency, a `sleep`,
a hard-coded literal — if the next reader would be tempted to simplify it, the comment says what
happened last time somebody did.
