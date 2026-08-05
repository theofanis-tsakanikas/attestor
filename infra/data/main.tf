# Data — Iceberg on S3, catalogued in Glue, queried by Athena.
#
# Iceberg rather than plain Parquet for one reason that matters more than the rest: snapshot
# isolation. Claim 4 says a report re-resolved as of an earlier instant produces identical
# figures, and that is a table-format property before it is an application property. Every
# lineage record pins the snapshot it read; without a format that keeps snapshots, the pin
# would be a comment.
#
# Athena rather than a warehouse because the workload is a handful of aggregate queries per
# report. A cluster would be idle capacity bought to avoid a per-query charge that is smaller
# than the cluster.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
  backend "s3" { key = "data/terraform.tfstate" }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "attestor:layer"      = "data"
      "attestor:managed"    = "terraform"
      "attestor:expires-at" = var.expires_at
    }
  }
}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "foundation/terraform.tfstate"
    region = var.region
  }
}

locals {
  lake = data.terraform_remote_state.foundation.outputs.lake_bucket
  kms  = data.terraform_remote_state.foundation.outputs.kms_key_arn
}

resource "aws_glue_catalog_database" "gold" {
  name        = "${var.project}_gold"
  description = "Published-figure sources. Everything a disclosure resolves through."
}

resource "aws_glue_catalog_database" "ref" {
  name        = "${var.project}_ref"
  description = "Reference data: emission factors, chart of accounts, category screening."
}

# The tables the committed queries read. Declared rather than crawled: a crawler infers a
# schema from whatever happens to be in the bucket, and a disclosure should not depend on
# what a crawler thought last Tuesday.
resource "aws_glue_catalog_table" "gold" {
  for_each = {
    ghg_scope_1_activity = [
      { name = "tenant_id", type = "string" },
      { name = "activity_date", type = "date" },
      { name = "co2e_tonnes", type = "decimal(18,4)" },
      { name = "consolidation_boundary", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    ghg_scope_3_activity = [
      { name = "tenant_id", type = "string" },
      { name = "activity_date", type = "date" },
      { name = "category", type = "string" },
      { name = "co2e_tonnes", type = "decimal(18,4)" },
      { name = "estimation_method", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    electricity_consumption = [
      { name = "tenant_id", type = "string" },
      { name = "reading_date", type = "date" },
      { name = "kwh", type = "decimal(18,4)" },
      { name = "reading_type", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    meter_interval_reading = [
      { name = "tenant_id", type = "string" },
      { name = "interval_start", type = "timestamp" },
      { name = "kwh", type = "decimal(18,6)" },
      { name = "dq_status", type = "string" },
    ]
    general_ledger_posting = [
      { name = "tenant_id", type = "string" },
      { name = "posting_date", type = "date" },
      { name = "account_code", type = "string" },
      { name = "amount_eur", type = "decimal(18,2)" },
      { name = "period_status", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    financial_statement_extract = [
      { name = "tenant_id", type = "string" },
      { name = "period_start", type = "date" },
      { name = "period_end", type = "date" },
      { name = "net_revenue_eur", type = "decimal(18,2)" },
      { name = "statement_status", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    procurement_fuel_spend = [
      { name = "tenant_id", type = "string" },
      { name = "invoice_date", type = "date" },
      { name = "fuel_type", type = "string" },
      { name = "net_amount_eur", type = "decimal(18,2)" },
      { name = "dq_status", type = "string" },
    ]
    # Rows that failed a data contract land here rather than being dropped, carrying the rule
    # they violated. A quarantined row is why E_UPSTREAM_QUARANTINE exists, and a figure
    # computed over an unexamined quarantine is a figure computed over missing data.
    quarantine = [
      { name = "tenant_id", type = "string" },
      { name = "source_table", type = "string" },
      { name = "rule", type = "string" },
      { name = "payload", type = "string" },
      { name = "quarantined_at", type = "timestamp" },
    ]
  }

  name          = each.key
  database_name = aws_glue_catalog_database.gold.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    table_type        = "ICEBERG"
    format            = "parquet"
    write_compression = "zstd"
  }

  storage_descriptor {
    location = "s3://${local.lake}/gold/${each.key}/"

    dynamic "columns" {
      for_each = each.value
      content {
        name = columns.value.name
        type = columns.value.type
      }
    }
  }
}

resource "aws_athena_workgroup" "main" {
  name          = var.project
  force_destroy = true

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true

    # A hard scan ceiling per query. The lake is small; a query that wants to read a terabyte
    # is a query with a missing predicate, and finding that out from a bill is expensive.
    bytes_scanned_cutoff_per_query = var.scan_ceiling_bytes

    result_configuration {
      output_location = "s3://${local.lake}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key           = local.kms
      }
    }
  }
}
