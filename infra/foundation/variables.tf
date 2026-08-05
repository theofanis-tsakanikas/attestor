variable "project" {
  type    = string
  default = "attestor"
}

variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "expires_at" {
  type        = string
  description = <<-EOT
    ISO date after which the reaper reports this estate as overdue. Supplied by the deploy
    workflow from its `days` input, so standing the estate up requires stating how long it is
    meant to live. There is no default: an estate with no expiry is the failure this tag
    exists to prevent.
  EOT
}

variable "log_retention_days" {
  type        = number
  default     = 14
  description = <<-EOT
    Matched to the estate's life rather than to a compliance minimum. A year of retention on
    logs about a VPC that existed for a week is a bill for archaeology.
  EOT
}
