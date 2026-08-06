# Day one

Everything in this repository is Terraform except the items below. Most are here because they
have no API; one is here for a better reason, stated in its own row. Each is recorded, dated
and initialled when it is done, so "who turned this on" has an answer a year from now.

| # | Task | Why it is not code | Lead time | Done |
|---|---|---|---:|:--:|
| 1 | **Bedrock model access for Anthropic** — submit the use-case form, then enable the reasoning model | There *is* an API (`put-use-case-for-model-access`, `create-foundation-model-agreement`), and it is deliberately not used — see below | hours to days | ☐ |
| 2 | **Service quotas** — raise Bedrock on-demand TPM/RPM for the reasoning model | Quota increases are ticketed and reviewed | days | ☐ |
| ~~3~~ | ~~Confirm AgentCore region availability~~ | **Done.** Verified available in `eu-central-1`, so the data plane and the agent plane stay in the same region and there is no residency split to document | — | ☑ |
| 4 | **External OIDC application** for `lumen` — register the app, note issuer, audience and groups claim | The tenant's identity provider is not ours to provision | minutes | ☐ |
| 5 | **Bootstrap apply** — `terraform -chdir=infra/bootstrap apply`, once, from a laptop with SSO | CI authenticates by assuming a role that has to exist first | minutes | ☐ |
| 6 | **GitHub Environments** `deploy` and `destroy`, with required reviewers | The OIDC trust is scoped to these; without them nothing can assume the role | minutes | ☐ |
| 7 | **Repository variables and secrets** — `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `TF_STATE_BUCKET`, `TF_LOCK_TABLE` | Outputs of step 5 | minutes | ☐ |
| 8 | **Budget alert address** confirmed (SNS subscription accepted) | An unconfirmed subscription silently drops every alert | minutes | ☐ |

## What the deploy workflow does that you should know about

**The agent layer applies twice.** ECR is created by that layer, the image cannot be built
before it exists, and AgentCore Runtime cannot start before the image is pushed. The workflow
applies with `deploy_runtime=false`, builds and pushes, then applies again with it on.
Collapsing this into one step is how a deploy fails at minute forty on a missing tag.

**The lake is seeded before any report runs.** An estate that stands up and resolves against
empty tables behaves perfectly correctly and produces nothing, which looks exactly like a
broken deploy and is much harder to diagnose. `pipelines/seed` refuses to write if any total
drifts from `recordings/`.

**A blocked tenant does not fail the deploy.** `aegis` is *supposed* to block — its Scope 1
misses its own cross-check by 4.3%. A workflow that went red on a correct refusal would teach
everyone to ignore it.

## Why step 1 is first, and why it is not automated

It is the only item with a lead time measured in days, and everything downstream is blocked
on it. Discovering on deploy day that an Anthropic model returns `AccessDeniedException`
because a form was never submitted is a lost day, and it is a well-known lost day.

Note the shape of the failure: model access is per account **and** per region, and the
console shows one region at a time. A run that works in `eu-central-1` and fails in
`us-east-1` is almost always this.

**It could be scripted, and it will not be.** `aws bedrock put-use-case-for-model-access`
submits the form and `aws bedrock create-foundation-model-agreement` accepts the offer, so
this is not a gap in the API. What that second call accepts is a *commercial agreement* —
the offer carries a rate card — and accepting pricing terms on an organisation's behalf is
the kind of act that needs a named human behind it. This repository already takes that
position where it matters most: no model, no agent and no service principal may request or
approve an override ([ADR-0001](adr/0001-fail-closed-with-a-recorded-key.md)). A signature on
a supplier agreement is not a weaker case than a signature on an omission.

So the row above says "not code" rather than "no API", because the first is true and the
second was not.

## Verified state of this account

Checked read-only against `387229419515` / `eu-central-1`. Re-check rather than trust this
table if time has passed — it is a snapshot, not a control.

| What | State |
|---|---|
| `amazon.titan-embed-text-v2:0` | **Ready.** Entitled, and Amazon's own models need no marketplace agreement |
| `anthropic.claude-haiku-4-5-...` | **Not enabled.** An unaccepted agreement offer is outstanding |
| Anthropic use-case form | **Not submitted** — `GetUseCaseForModelAccess` returns `ResourceNotFoundException` |
| AgentCore control plane | **Available** in `eu-central-1` |
| `attestor:managed` resources | **0** — nothing left over from a previous run |

The one that blocks a deploy is the Anthropic pair. Titan needs nothing.

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
