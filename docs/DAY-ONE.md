# Day one

Everything in this repository is Terraform except the items below, and these are here because
they have no API — not because somebody preferred a console. Each one is recorded, dated and
initialled when it is done, so "who turned this on" has an answer a year from now.

| # | Task | Why it cannot be code | Lead time | Done |
|---|---|---|---:|:--:|
| 1 | **Bedrock model access** — enable Anthropic Claude and Amazon Titan Embeddings in the account, per region | Enabling a model family is a console action; Anthropic additionally requires a one-time use-case submission | hours to days | ☐ |
| 2 | **Service quotas** — raise Bedrock on-demand TPM/RPM for the reasoning model | Quota increases are ticketed and reviewed | days | ☐ |
| 3 | **Confirm AgentCore region availability** | Availability changes; it is not something to discover mid-demo | minutes | ☐ |
| 4 | **External OIDC application** for `lumen` — register the app, note issuer, audience and groups claim | The tenant's identity provider is not ours to provision | minutes | ☐ |
| 5 | **Bootstrap apply** — `terraform -chdir=infra/bootstrap apply`, once, from a laptop with SSO | CI authenticates by assuming a role that has to exist first | minutes | ☐ |
| 6 | **GitHub Environments** `deploy` and `destroy`, with required reviewers | The OIDC trust is scoped to these; without them nothing can assume the role | minutes | ☐ |
| 7 | **Repository variables and secrets** — `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `TF_STATE_BUCKET`, `TF_LOCK_TABLE` | Outputs of step 5 | minutes | ☐ |
| 8 | **Budget alert address** confirmed (SNS subscription accepted) | An unconfirmed subscription silently drops every alert | minutes | ☐ |

## Why step 1 is first

It is the only item with a lead time measured in days, and everything downstream is blocked
on it. Discovering on deploy day that a streaming Anthropic model returns
`AccessDeniedException` because a form was never submitted is a lost day, and it is a
well-known lost day.

Note the shape of the failure: model access is per account **and** per region, and the
console shows one region at a time. A run that works in `eu-central-1` and fails in
`us-east-1` is almost always this.

## Step 3, and the decision behind it

The data plane is European on purpose: the evidence corpus contains a European
undertaking's records, and CSRD reporting is a European obligation. If AgentCore is not
available in the data plane's region, there is a decision to make, and it is not a
technicality:

| Option | What it costs |
|---|---|
| Move everything to an AgentCore region | The lakehouse and the evidence corpus leave the EU. For this domain that is usually disqualifying |
| Split: data plane in the EU, agent plane elsewhere | A cross-region hop for prompts and retrieved passages — the passages are the sensitive part, not the prompts. Documentable, but it must be documented |
| Wait, and run the classic Bedrock Agent path meanwhile | Loses Gateway, Identity and Memory; the tool handlers are unchanged because they are plain handlers behind an OpenAPI description |

The third is why the tools were written as ordinary Python behind a generated OpenAPI
description rather than against an AgentCore SDK. It keeps this a two-way door.

## What is deliberately not on this list

**Nothing about creating resources.** No console click creates anything in this project. If a
step here starts with "in the console, create…", it is a bug in the design, not a task.

**No long-lived credentials, anywhere.** SSO for people, OIDC for CI, execution roles for
services. There is no step that produces an access key, because there is no place to put one.
