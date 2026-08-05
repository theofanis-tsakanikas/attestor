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

variable "scan_ceiling_bytes" {
  type        = number
  default     = 10737418240 # 10 GiB
  description = "Per-query scan ceiling. A query that exceeds it has a missing predicate."
}
