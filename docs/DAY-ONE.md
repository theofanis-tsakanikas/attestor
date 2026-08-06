# Day one

Everything in this repository is Terraform except the items below. Most are here because they
have no API; one is here for a better reason, stated in its own row. Each is recorded, dated
and initialled when it is done, so "who turned this on" has an answer a year from now.

| # | Task | Why it is not code | Lead time | Done |
|---|---|---|---:|:--:|
| ~~0~~ | ~~Push the repository to GitHub~~ | **Done.** Private, `theofanis-tsakanikas/attestor`. Everything below depends on it: the OIDC trust is scoped to `repo:<owner>/<repo>` | — | ☑ |
| ~~1~~ | ~~Bedrock model access for Anthropic~~ | **Done.** Use-case form submitted and `eu.anthropic.claude-haiku-4-5-20251001-v1:0` verified to respond from the CLI, in `eu-central-1` | — | ☑ |
| 2 | **Service quotas** — raise Bedrock on-demand TPM/RPM for the reasoning model | Quota increases are ticketed and reviewed | days | ☐ |
| ~~3~~ | ~~Confirm AgentCore region availability~~ | **Done.** Verified available in `eu-central-1`, so the data plane and the agent plane stay in the same region and there is no residency split to document | — | ☑ |
| 4 | **External OIDC application** for `lumen` — register the app, note issuer, audience and groups claim | The tenant's identity provider is not ours to provision | minutes | ☐ |
| ~~5~~ | ~~Bootstrap apply~~ | **Done.** 19 resources in `<account>` / `eu-central-1`; the backend, the deploy role and `/attestor/bootstrap/*` all stand | — | ☑ |
| ~~6~~ | ~~GitHub Environments `deploy` and `destroy`~~ | **Done, without reviewers** — the plan does not allow them on a private repository. See the section below; this one is not merely ticked | — | ☑ |
| ~~7~~ | ~~Two repository variables~~ | **Done.** `AWS_ACCOUNT_ID` and `AWS_REGION`. No secret | — | ☑ |
| 8 | **Budget alert address** confirmed | Two notification subscriptions were created for `theotsak90@gmail.com`; an unconfirmed one silently drops every alert | minutes | ☐ |

## Step 0 is not a formality

`infra/bootstrap` takes `github_repository` as a required variable with no default, and it
is the single most consequential string in the layer: the OIDC trust condition is

    repo:<owner>/<repo>:environment:deploy

A wrong value there does not fail loudly. It creates a role whose trust policy names a
repository that is not yours — so every workflow run fails at `configure-aws-credentials`
with an error about assuming a role, and the cause is three layers away from the symptom. It
also means that if the named repository ever exists and its owner adds a `deploy`
environment, they can assume the role.

So the repository has to exist, and its owner/name has to be known, before this layer is
applied. That ordering was implicit and is now written down.

## Why CI carries two values and not five

Four things used to be transcribed from `terraform output` into repository settings: a state
bucket, a lock table, a role ARN and a region. Three of them were not facts. They were
consequences of names `infra/bootstrap` had already chosen —
`attestor-tfstate-<account>`, `attestor-tfstate-locks`,
`arn:aws:iam::<account>:role/attestor-github-deploy` — and copying a derived value into a
settings page makes it look like an independent knob. Rename the bucket and the deploy fails
on a backend nobody can find, with the fix living in a web form rather than in a diff.

So bootstrap publishes them to `/attestor/bootstrap/*` and the workflows read them once
credentials exist. The role ARN is derived from the account id in the `role-to-assume` line
itself, which is why there is no longer a repository *secret* at all.

What cannot be published is the account id: CI has to know **which** account before it can
ask that account anything. That is the irreducible one, and it is a variable rather than a
secret because an AWS account id is not one — the boundary is the OIDC trust policy, scoped
to a single repository and a single environment.

## Step 6 is ticked and the gate is still missing

The environments exist, so the OIDC subject resolves and CI can assume the role. What does
**not** exist is the thing they were chosen for. Required reviewers are a paid protection
rule: GitHub offers them free on public repositories and on private ones only under Pro,
Team or Enterprise. This repository is private on a Free account, and the API says so
plainly — `Failed to create the environment protection rule. Please ensure the billing plan
supports the required reviewers protection rule.`

So the claim in `variables.tf` that "a workflow that has not passed the environment's
reviewers cannot mint credentials at all" is, on this account, not true. It is worth being
exact about what survives and what does not:

| Property | Still holds? |
|---|---|
| Only `deploy` and `destroy` jobs in **this** repository can assume the role | **Yes** — that is the `sub` condition, and it does not depend on the plan |
| Nothing applies on a push; both workflows are `workflow_dispatch` only | **Yes** |
| `destroy` requires the word `DESTROY` typed into the dispatch form | **Yes** |
| A **second person** must approve before credentials are minted | **No.** This is what the plan withholds |

For a single-maintainer repository the fourth row was never going to be a second person
anyway, so the honest description is that the gate is deliberate rather than reviewed. Three
ways to get the real thing, in order of what they cost:

1. **Make the repository public.** Protection rules are free there. Reasonable for a
   portfolio piece — but read the repository as an attacker first, because the OIDC trust
   policy, the account id and the workflow surface all become public with it.
2. **GitHub Pro**, a few euro a month, keeps it private and restores the rule.
3. **Leave it.** Then this section is the record of what is missing, which is the point of
   writing it down rather than ticking the row and moving on.

## Two things the bootstrap apply found

Neither was visible in a plan. Both are recorded because the next account will hit them.

**The GitHub OIDC provider already existed.** An account holds at most one provider per
issuer, and the endpoint is shared by every repository that federates in — `dbx-github-deploy`
got there on 2026-07-04. `CreateOpenIDConnectProvider` returned `EntityAlreadyExists`. The
fix is `create_github_oidc_provider = false`, which binds the deploy role to the provider
already standing and derives its ARN from the account id rather than asking anyone to copy
one. Importing it into this state would have been the tempting alternative and the wrong one:
a later `terraform destroy` of this layer would then delete a provider another repository's
CI depends on, and it would happen without warning.

**AWS Budgets only speaks dollars.** `limit_unit = "EUR"` is rejected outright —
`EUR is not in the supported unit set: [USD]`. The variable is now `budget_usd`. The
per-tenant cost meter stays in euro, because that is our own arithmetic over our own price
table and it reports what a European client is billed; the AWS-side ceiling is in dollars
because AWS gives no choice.

## Where the bootstrap state lives

On the laptop that applied it, at `infra/bootstrap/terraform.tfstate`, gitignored. That is
deliberate and it has a consequence worth naming: **lose the laptop and this layer becomes
unmanaged.** Nothing breaks — the role, the bucket and the parameters keep working, and every
other layer keeps its state in S3 — but changing bootstrap again would mean importing four
resources by hand.

The obvious remedy is a `backend "s3"` block pointing at the bucket bootstrap creates, and it
is deliberately not there: a layer whose backend is the bucket it has not created yet cannot
bootstrap a fresh account without someone commenting the block out first. The chicken-and-egg
is real, and the choice is between a layer that is awkward to recover and one that is awkward
to start. Back the file up out of band and the first problem goes away; nothing makes the
second one go away.

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

## How step 1 actually works now

**The "Model access" page has been retired.** There is no longer a list of checkboxes to
tick. Serverless foundation models are enabled automatically the first time they are invoked
in an account, with two exceptions that both apply here:

- **Anthropic models** ask a first-time user to submit use-case details before access is
  granted. `aws bedrock get-use-case-for-model-access` returns `ResourceNotFoundException`
  until that has happened, which makes it a precise, credential-free way to check.
- **Marketplace-served models** are enabled account-wide by *one* invocation from a principal
  holding AWS Marketplace permissions. The first invoke is the act of enabling.

So the sequence is: open the model in the Bedrock **Model catalog**, run one prompt in the
playground, fill in the use-case form when it appears, and the successful response is the
confirmation. Not a checkbox — a first call.

Region still matters exactly as much as it did. The enablement is per account **and** per
region, so the console must be in `eu-central-1` when this is done.

## Why step 1 is first, and why it is not automated

It is the only item with a lead time measured in days, and everything downstream is blocked
on it. Discovering on deploy day that an Anthropic model returns `AccessDeniedException`
because a form was never submitted is a lost day, and it is a well-known lost day.

Note the shape of the failure: model access is per account **and** per region, and the
console shows one region at a time. A run that works in `eu-central-1` and fails in
`us-east-1` is almost always this.

**It could be scripted, and it will not be.** `aws bedrock put-use-case-for-model-access`
submits the form and `aws bedrock create-foundation-model-agreement` accepts the offer, so
this is not a gap in the API. Two reasons it stays manual anyway.

The form asks who the undertaking is — company, website, industry, intended use. Those are
facts about the organisation, not about this repository, and a script that fills them is a
script that asserts them.

And the enabling act is a commercial one: the agreement offer carries a rate card, and under
the current flow the first invocation is what accepts it account-wide. Accepting pricing
terms on an organisation's behalf wants a named human behind it. This repository already
takes that position where it matters most — no model, no agent and no service principal may
request or approve an override ([ADR-0001](adr/0001-fail-closed-with-a-recorded-key.md)) —
and a signature on a supplier agreement is not a weaker case than a signature on an
omission.

So the row above says "not code" rather than "no API", because the first is true and the
second was not.

## Verified state of this account

Checked read-only against `<account>` / `eu-central-1`. Re-check rather than trust this
table if time has passed — it is a snapshot, not a control.

| What | State |
|---|---|
| `amazon.titan-embed-text-v2:0` | **Ready.** Entitled, and Amazon's own models need no marketplace agreement |
| `eu.anthropic.claude-haiku-4-5-...` | **Ready.** Verified by a `converse` call against the exact profile id `infra/agent` pins |
| Anthropic use-case form | **Submitted** |
| AgentCore control plane | **Available** in `eu-central-1` |
| State backend | `attestor-tfstate-<account>`, versioned, SSE-KMS, public access blocked |
| Lock table | `attestor-tfstate-locks`, PITR on |
| Deploy role | `attestor-github-deploy`, trusting only `…:environment:deploy` and `…:environment:destroy` |
| `/attestor/bootstrap/*` | Three parameters, read by both workflows before anything else |
| Budget | `attestor-estate`, 300 USD monthly, with an IAM-deny action at the ceiling |
| `attestor:managed` resources | Bootstrap only — no estate has been stood up yet |

Note which of those two model rows was work. Amazon's own models are simply available, which
is why a previous project in this account used Bedrock without ever meeting this step:
`amazon.titan-embed-text-v2:0` and `eu.amazon.nova-lite-v1:0` appear in CloudWatch, and
neither needs an agreement. The gate is Anthropic's, not AWS's, and it is per account rather
than per project.

**`agreementAvailability` is not the access signal.** It reads `NOT_AVAILABLE` for an
Anthropic model both before and after access is granted — it describes whether an offer can
be created, not whether you may invoke. The signal that means something is an actual
`converse` call returning text.

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
