variable "project" {
  type    = string
  default = "attestor"
}
variable "region" {
  type    = string
  default = "eu-central-1"
}
variable "state_bucket" {
  type = string
}
variable "expires_at" {
  type = string
}

variable "agent_region" {
  type        = string
  default     = "eu-central-1"
  description = <<-EOT
    Where the AgentCore control plane lives. Separate from `region` because AgentCore
    availability may not match the data plane's. If they diverge, the split is a documented
    data-residency decision — see docs/DAY-ONE.md — not something to find out mid-demo.
  EOT
}

variable "cognito_tenants" {
  type        = list(string)
  default     = ["helios", "aegis"]
  description = <<-EOT
    One user pool each. `lumen` is deliberately absent: it authenticates against an external
    OIDC provider, which is what makes 'identity is per tenant' a property of the design
    rather than of the configuration.
  EOT
}

variable "role_groups" {
  type    = list(string)
  default = ["reporting", "preparers", "assurance"]
}

variable "callback_urls" {
  type    = list(string)
  default = ["https://localhost:8443/callback"]
}

variable "reasoning_model" {
  type        = string
  default     = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
  description = <<-EOT
    A cross-region inference profile, EU-resident, and deliberately not the largest model
    available.

    What the model does here is narrow: three narrative datapoints, each a few hundred words
    of grounded prose that must cite retrieved passages, must carry no digit, and must come
    back as JSON with five named keys. It never produces a figure, never decides an
    abstention and never authorizes anything — `contracts/model.py` makes the first a type
    error and Cedar makes the third a policy.

    That narrowness is what decides the tier. Every way this model can fail is caught
    structurally: a digit fails the manifest and then the provenance gate, an invented
    citation fails `check_draft`, prose where JSON was demanded raises rather than being
    re-prompted into something softer. So a weaker model does not produce a wrong report —
    it produces a blocked one. The choice is about how often a build stops, not about
    whether a number can be trusted, and it is a two-way door: raising it is this line and
    a re-run.

    **This is that re-run.** The line said Haiku 4.5 and the empirical answer came back: it
    blocked every narrative datapoint on every tenant, across five deploys, and never once
    produced a draft that passed. Not for one reason — it numbered its levers, wrote section
    references into prose, cited one passage where three were demanded and then none at all,
    and ran past the word ceiling. Each of those is a rule the prompt states plainly; holding
    four absolute rules at once is what it could not do.

    The prediction above held exactly, which is the part worth keeping. Nothing wrong was
    published. The system refused, said why, and the refusals were legible enough to diagnose
    from. A tier is a decision about how often a build stops, and this one stopped always.

    Note where the money actually is. A report run makes a handful of model calls; the
    estate's dominant cost is OpenSearch Serverless sitting idle. Choosing a cheaper model
    is right because it fits the task, not because it moves the bill.

    Anthropic models are gated behind a one-time account approval, per account *and* per
    region — a Day-1 task, not a deploy-time surprise.
  EOT
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "denial_alarm_threshold" {
  type        = number
  default     = 20
  description = "Denials in five minutes. Alarms on the rate: alerting on each one trains people to ignore it."
}

variable "memory_retention_days" {
  type        = number
  default     = 30
  description = <<-EOT
    How long AgentCore Memory keeps an event. Short on purpose: agent memory holds fragments
    of a tenant's reporting conversation, and a store that keeps them indefinitely becomes a
    second copy of the evidence corpus with none of its controls.
  EOT
}

variable "deploy_runtime" {
  type        = bool
  default     = false
  description = <<-EOT
    Whether to create the AgentCore Runtime. It needs an image in ECR, so the deploy workflow
    builds and pushes first and then applies with this on. Split so that standing up Gateway,
    the policy engine and memory does not wait on a container build.
  EOT
}

variable "agent_image_tag" {
  type        = string
  default     = "latest"
  description = "Image tag to run. The deploy workflow passes the commit sha; `latest` is a local convenience."
}

variable "surface_roles" {
  type        = map(string)
  default     = { helios = "role:preparer", aegis = "role:preparer" }
  description = <<-EOT
    The role a call through each tenant's AgentCore surfaces carries.

    AgentCore invokes a Lambda target with the gateway's own IAM role and forwards none of the
    caller's token — confirmed from the client context, which carries the tool name, the gateway
    id and the target id, and no claims at all. So the handler can derive the tenant, because
    there is one gateway per tenant and AgentCore asserts which one was called, and it cannot
    derive the role.

    Declaring it here is the alternative to inventing it. A role in this map is a sentence
    somebody wrote and somebody reviewed — "a call through the helios gateway is a preparer" —
    rather than a default a handler chose at runtime. Cedar still runs twice: at the edge with
    the caller's real claims, where it filters `tools/list` and refuses `request_override`, and
    again inside the Lambda against this declared role.

    Widening one of these is a pull request, which is the only way this project lets authority
    change.
  EOT
}
