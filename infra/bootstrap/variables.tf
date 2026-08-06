variable "project" {
  type        = string
  default     = "attestor"
  description = "Prefix for every resource name in the account."
}

variable "region" {
  type        = string
  default     = "eu-central-1"
  description = "Where state and identity live. The estate may span regions; this does not."
}

variable "github_repository" {
  type        = string
  description = "owner/repo. The OIDC trust is scoped to exactly this repository."

  validation {
    condition     = length(split("/", var.github_repository)) == 2
    error_message = "github_repository must be exactly owner/repo."
  }
}

variable "github_owner_id" {
  type        = string
  description = <<-EOT
    The owner's numeric id, from `gh api users/<owner> --jq .id`.

    GitHub issues an immutable subject claim — `repo:owner@<owner id>/repo@<repo id>:...` —
    because a name can be released and re-registered by somebody else while an id cannot. A
    trust scoped to names alone is inheritable by whoever claims the name after the
    repository is deleted, and that is the whole point of the format.

    There is no default and there cannot be one: this is a fact about an account, and a
    wrong value fails the way the first deploy here failed — `Not authorized to perform
    sts:AssumeRoleWithWebIdentity`, a message that names none of its causes.
  EOT
}

variable "github_repository_id" {
  type        = string
  description = <<-EOT
    The repository's numeric id, from `gh api repos/<owner>/<repo> --jq .id`.
    See `github_owner_id` for why ids and not names.
  EOT
}

variable "budget_usd" {
  type        = string
  default     = "300"
  description = <<-EOT
    Monthly ceiling. At 100% the deploy role has a deny policy attached, so the estate cannot
    grow. Chosen well above the expected spend of a bounded run: a guard that trips during
    normal work gets raised until it never trips at all.

    US dollars, because AWS Budgets accepts no other unit — the API rejects `EUR` outright.
    The per-tenant cost meter stays in euro: that one is our own arithmetic over our own
    price table, and it reports the number a European client is actually billed.
  EOT
}

variable "create_github_oidc_provider" {
  type        = bool
  default     = true
  description = <<-EOT
    Whether this layer creates the GitHub OIDC provider, or binds to one already in the
    account. The endpoint is account-global and shared by every repository that federates
    here, so the second project into an account must adopt rather than create — AWS returns
    `EntityAlreadyExists`, and a provider imported into this state would be deleted by a
    `destroy` that another repository's CI never agreed to.

    `true` is the fresh-account default. It is set to `false` in `terraform.tfvars` for the
    account this repository is deployed into, where `dbx-github-deploy` got there first.
  EOT
}

variable "github_oidc_thumbprints" {
  type        = list(string)
  default     = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
  description = <<-EOT
    Only read when this layer creates the provider. AWS stopped verifying thumbprints for
    IdPs with a publicly trusted certificate, but the field is still required, so the value
    is pinned rather than left to drift.
  EOT
}

variable "budget_alert_email" {
  type        = string
  description = "Where budget notifications go."
}
