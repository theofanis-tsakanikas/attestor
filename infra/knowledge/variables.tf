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
  type        = string
  description = <<-EOT
    The role CI assumes. It goes into the OpenSearch data access policy so that `kb-sync.sh`
    and the ingestion jobs can reach the collection — the knowledge base's own role is not
    enough, because the workflow talks to the index directly.

    The validation is here because AOSS reports a bad principal by printing the six ARN
    patterns it would have accepted, and the knowledge base then fails separately with a 401
    about "storage configuration". Three errors, none of which say "this string was empty".
  EOT

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/.+$", var.deploy_role_arn))
    error_message = "deploy_role_arn must be an IAM role ARN. Empty usually means the workflow never resolved it from /attestor/bootstrap/deploy_role_arn."
  }
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

variable "embedding_dimension" {
  type        = number
  default     = 1024
  description = <<-EOT
    How many floats the embedding model emits. Titan Text Embeddings V2 emits 1024.

    It is a variable rather than a constant in the indexer because it has to move with
    `embedding_model`, and the failure when it does not is quiet: the index is created, the
    knowledge base is created, and the first ingestion job fails on a dimension mismatch —
    long after the apply that caused it went green.
  EOT
}

variable "production_topology" {
  type        = bool
  default     = false
  description = <<-EOT
    Cross-AZ redundancy for the vector store, which doubles its billed capacity.

    Off by default, and the default is a statement about this estate rather than about the
    setting: it is ephemeral, it is rebuilt from this configuration in half an hour, and
    redundancy protects availability that a run nobody depends on does not have. On for any
    run that has to stand up the way a deployment serving real traffic would.

    A variable rather than a constant so the topology is declared per run and visible in the
    dispatch, instead of being whatever the last person left behind.
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
