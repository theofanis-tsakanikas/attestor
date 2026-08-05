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
}

variable "deploy_environments" {
  type        = list(string)
  default     = ["deploy", "destroy"]
  description = <<-EOT
    GitHub Environments allowed to assume the deploy role. Scoping the OIDC subject to an
    environment rather than to a branch is what makes the approval gate meaningful: a
    workflow that has not passed the environment's reviewers cannot mint credentials at all.
  EOT
}

variable "budget_eur" {
  type        = string
  default     = "300"
  description = <<-EOT
    Monthly ceiling. At 100% the deploy role has a deny policy attached, so the estate cannot
    grow. Chosen well above the expected spend of a bounded run: a guard that trips during
    normal work gets raised until it never trips at all.
  EOT
}

variable "github_oidc_thumbprints" {
  type    = list(string)
  default = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

variable "budget_alert_email" {
  type        = string
  description = "Where budget notifications go."
}
