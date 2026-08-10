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

# Cross-layer references. Read as named SSM parameters the producing layer publishes —
# never as that layer's state file. A `terraform_remote_state` data source needs read access
# to the whole state bucket and exposes every attribute of every resource in it; a parameter
# path exposes exactly what its owner chose to offer.
data "aws_ssm_parameter" "foundation" {
  for_each = toset([
    "vpc_id",
    "private_subnet_ids",
    "endpoint_security_group_id",
    "kms_key_arn",
    "lake_bucket",
    "evidence_bucket",
    "reports_bucket",
    "alerts_topic_arn",
  ])

  name = "/${var.project}/foundation/${each.value}"
}

locals {
  foundation = { for key, param in data.aws_ssm_parameter.foundation : key => param.value }
}

locals {
  lake = local.foundation.lake_bucket
  kms  = local.foundation.kms_key_arn
}

# There is no `ref` database here. dbt owns the reference tables, and `+schema: ref` puts
# them in `<database>_ref` — so a Terraform-declared `attestor_ref` was a third name for a
# thing with two, permanently empty, and the queries never looked at it.
resource "aws_glue_catalog_database" "gold" {
  name        = "${var.project}_gold"
  description = "Published-figure sources. Everything a disclosure resolves through."
}

# The gold tables **the application writes**. That is the whole of what belongs here.
#
# Eleven analytical tables used to be declared alongside these, and none of them had a
# producer: `pipelines/dbt` had five models where the queries read eleven tables. So the
# catalogue described a lakehouse that existed only in Glue, `dbt build` failed on the first
# live run, and every offline check stayed green because the resolver replays `recordings/`
# and never touches gold at all.
#
# They are dbt's now, and declared once rather than twice. Two owners for one table is how a
# schema ends up correct in Terraform and different in Athena, with each side pointing at the
# other. `report_run` and `report_datapoint` stay because the report writer inserts into them
# directly — no dbt model produces them, so nothing else would create them.
#
# Destroy is unaffected: dropping `aws_glue_catalog_database.gold` takes every table in it,
# including the ones dbt made, and the teardown empties the bucket underneath.
#
# Declared rather than crawled: a crawler infers a schema from whatever happens to be in the
# bucket, and a disclosure should not depend on what a crawler thought last Tuesday.
resource "aws_glue_catalog_table" "gold" {
  for_each = {
    # One row per report run, and one per datapoint within it. This is what the analytics
    # views read, and it is the only place a trend across periods can be asked about — a
    # per-run JSON beside the artefacts answers "what happened", never "is it getting worse".
    report_run = [
      { name = "run_id", type = "string" },
      { name = "tenant_id", type = "string" },
      { name = "standard", type = "string" },
      { name = "period", type = "string" },
      { name = "started_at", type = "string" },
      { name = "finished_at", type = "string" },
      { name = "issued", type = "boolean" },
      { name = "published_count", type = "int" },
      { name = "limitation_count", type = "int" },
      { name = "blocker_count", type = "int" },
      { name = "artefact_count", type = "int" },
      { name = "cost_eur", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    # Published figures and omissions share one table, distinguished by `disclosed`. Splitting
    # them would make "what did we not disclose, and why" a join, and the omissions register
    # has to be as easy to read as the figures.
    report_datapoint = [
      { name = "run_id", type = "string" },
      { name = "tenant_id", type = "string" },
      { name = "period", type = "string" },
      { name = "datapoint_id", type = "string" },
      { name = "reference", type = "string" },
      { name = "disclosed", type = "boolean" },
      { name = "value", type = "string" },
      { name = "unit", type = "string" },
      { name = "lineage_id", type = "string" },
      { name = "resolver_kind", type = "string" },
      { name = "reason_code", type = "string" },
      { name = "outcome", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    # The AI Act vertical's sources.
    # Rows that failed a data contract land here rather than being dropped, carrying the rule
    # they violated. A quarantined row is why E_UPSTREAM_QUARANTINE exists, and a figure
    # computed over an unexamined quarantine is a figure computed over missing data.
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

# The raw landing zone the seed writes into and dbt reads from.
#
# JSON rather than Parquet, and external rather than Iceberg: raw is what arrived, and what
# arrived should be legible without a reader. The Iceberg tables above are what dbt produces
# *from* this, which is the shape a real ingestion has — the transformation is the thing with
# a contract, not the landing.
resource "aws_glue_catalog_database" "raw" {
  name        = "${var.project}_raw"
  description = "Landing zone. Written by ingestion, read only by dbt staging models."
}

resource "aws_glue_catalog_table" "raw" {
  for_each = {
    electricity_consumption = [
      { name = "site_id", type = "string" },
      { name = "reading_date", type = "string" },
      { name = "kwh", type = "string" },
      { name = "reading_type", type = "string" },
      { name = "source_document_id", type = "string" },
      { name = "dq_status", type = "string" },
      { name = "ingested_at", type = "string" },
    ]
    meter_interval_reading = [
      { name = "interval_start", type = "string" },
      { name = "kwh", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    ghg_scope_1_activity = [
      { name = "activity_date", type = "string" },
      { name = "co2e_tonnes", type = "string" },
      { name = "consolidation_boundary", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    ghg_scope_3_activity = [
      { name = "activity_date", type = "string" },
      { name = "category", type = "string" },
      { name = "co2e_tonnes", type = "string" },
      { name = "estimation_method", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    procurement_fuel_spend = [
      { name = "invoice_date", type = "string" },
      { name = "fuel_type", type = "string" },
      { name = "net_amount_eur", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    general_ledger_posting = [
      { name = "posting_date", type = "string" },
      { name = "account_code", type = "string" },
      { name = "amount_eur", type = "string" },
      { name = "period_status", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    financial_statement_extract = [
      { name = "period_start", type = "string" },
      { name = "period_end", type = "string" },
      { name = "net_revenue_eur", type = "string" },
      { name = "statement_status", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    model_evaluation_prediction = [
      { name = "evaluated_at", type = "string" },
      { name = "example_id", type = "string" },
      { name = "predicted_label", type = "string" },
      { name = "true_label", type = "string" },
      { name = "is_held_out", type = "boolean" },
      { name = "dq_status", type = "string" },
    ]
    model_evaluation_confusion = [
      { name = "evaluated_at", type = "string" },
      { name = "predicted_label", type = "string" },
      { name = "true_label", type = "string" },
      { name = "count", type = "bigint" },
      { name = "dq_status", type = "string" },
    ]
    risk_register = [
      { name = "assessed_at", type = "string" },
      { name = "risk_id", type = "string" },
      { name = "mitigation_status", type = "string" },
      { name = "residual_rating", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    incident_log = [
      { name = "occurred_at", type = "string" },
      { name = "incident_id", type = "string" },
      { name = "classification", type = "string" },
      { name = "dq_status", type = "string" },
    ]
    # One row per labelled passage the retrieval scanner judged. The only stream in this lake
    # whose rows are a measurement of this system rather than a stand-in for a client's ERP
    # extract — `pipelines/seed` runs the scanner and writes what it decided.
    security_scan_result = [
      { name = "assessed_at", type = "string" },
      { name = "example_id", type = "string" },
      { name = "corpus", type = "string" },
      { name = "true_label", type = "string" },
      { name = "predicted_label", type = "string" },
      { name = "dq_status", type = "string" },
    ]
  }

  name          = each.key
  database_name = aws_glue_catalog_database.raw.name
  table_type    = "EXTERNAL_TABLE"

  # Partitioned by tenant. Not for speed — the data is tiny — but so that a query without a
  # tenant predicate scans one partition's worth of nothing rather than every tenant's rows.
  partition_keys {
    name = "tenant_id"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${local.lake}/raw/${each.key}/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.openx.data.jsonserde.JsonSerDe"
      parameters = {
        "ignore.malformed.json" = "false"
      }
    }

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
        kms_key_arn       = local.kms
      }
    }
  }
}
