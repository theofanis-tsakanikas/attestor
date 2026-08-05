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
    Sonnet, not Haiku. The work is interpreting a standard and refusing to overstate what
    evidence supports, which is judgement rather than throughput. Anthropic models are gated
    behind a one-time account approval — a Day-1 task, not a deploy-time surprise.
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
