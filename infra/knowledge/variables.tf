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
variable "deploy_role_arn" {
  type = string
}

variable "tenants" {
  type        = list(string)
  default     = ["helios", "aegis", "lumen"]
  description = "One S3 data source per tenant, all into one metadata-filtered index."
}

variable "embedding_model" {
  type        = string
  default     = "amazon.titan-embed-text-v2:0"
  description = <<-EOT
    A one-way door: changing it re-embeds the corpus. The bake-off decides it on a sample,
    and until the estate has run once, the comparison against Cohere is honestly reported as
    pending rather than invented.
  EOT
}

variable "production_topology" {
  type        = bool
  default     = false
  description = <<-EOT
    Standby replicas double OpenSearch capacity. Off during build blocks, on for the capture
    run — the screenshots should show the topology anyone would actually deploy.
  EOT
}

variable "grounding_threshold" {
  type    = number
  default = 0.8
}

variable "relevance_threshold" {
  type    = number
  default = 0.7
}
