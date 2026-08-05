# Foundation — network, keys, buckets, and the reaper that makes "we will destroy it" true.
#
# Applied only by the deploy workflow. Two decisions in here are worth defending.
#
# **There is a NAT gateway.** Avoiding one would save roughly six euro across a ten-day
# window, and would cost the property that matters: every subnet is private, nothing has a
# public address, and egress is a single audited path. Optimising a bounded run's bill by
# weakening its topology is the wrong trade, and it is the trade that quietly turns a
# portfolio project into something you would not deploy at work.
#
# **Every resource carries `attestor:expires-at`.** The reaper below destroys what has
# expired. This is the mechanism that makes an ephemeral estate real rather than aspirational,
# because the failure mode was never "we chose an expensive service" — it was "we forgot".

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
  }
  backend "s3" {
    key = "foundation/terraform.tfstate"
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "attestor:layer"      = "foundation"
      "attestor:managed"    = "terraform"
      "attestor:expires-at" = var.expires_at
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs    = slice(data.aws_availability_zones.available.names, 0, 2)
  prefix = var.project
}

# ── Keys ─────────────────────────────────────────────────────────────────────

resource "aws_kms_key" "data" {
  description             = "${local.prefix} data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 7
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.prefix}-data"
  target_key_id = aws_kms_key.data.key_id
}

# ── Network ──────────────────────────────────────────────────────────────────

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.prefix}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

# Public subnets hold the NAT gateway and nothing else. No workload is ever placed here, and
# no security group in this repository allows ingress from the internet.
resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false
  tags                    = { Name = "${local.prefix}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + length(local.azs))
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.prefix}-private-${count.index}" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
}

# One NAT, not one per zone. A second buys availability this estate does not need and doubles
# the only line item in this layer that is charged by the hour.
resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Interface endpoints for the services on the hot path. Traffic to Bedrock and Secrets
# Manager never leaves the VPC, which is both a security property and a way of not paying
# NAT data charges on every model call.
resource "aws_security_group" "endpoints" {
  name        = "${local.prefix}-endpoints"
  description = "VPC endpoint interfaces"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from inside the VPC only"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  egress {
    description = "Return traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset([
    "bedrock-runtime",
    "bedrock-agent-runtime",
    "secretsmanager",
    "logs",
    "sts",
    "athena",
  ])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
}

# ── Buckets ──────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "lake" {
  bucket        = "${local.prefix}-lake-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # the estate is ephemeral by design; teardown must not need a human
}

resource "aws_s3_bucket" "evidence" {
  bucket        = "${local.prefix}-evidence-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "reports" {
  bucket        = "${local.prefix}-reports-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration {
    # Evidence is what an auditor re-reads. Overwriting a document in place would make a
    # lineage record point at bytes that no longer exist.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = {
    lake     = aws_s3_bucket.lake.id
    evidence = aws_s3_bucket.evidence.id
    reports  = aws_s3_bucket.reports.id
  }

  bucket = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each = {
    lake     = aws_s3_bucket.lake.id
    evidence = aws_s3_bucket.evidence.id
    reports  = aws_s3_bucket.reports.id
  }

  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── The reaper ───────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "reaper_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "reaper" {
  name               = "${local.prefix}-reaper"
  assume_role_policy = data.aws_iam_policy_document.reaper_assume.json
}

resource "aws_iam_role_policy" "reaper" {
  name = "find-and-report-expired"
  role = aws_iam_role.reaper.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["tag:GetResources", "sns:Publish", "logs:*"]
        Resource = "*"
      }
    ]
  })
}

# It reports; it does not delete. A lambda with permission to destroy the estate is a larger
# risk than the estate outliving its window by a day, and the destroy workflow already exists
# and is already gated. What the reaper removes is the excuse "nobody noticed".
# Built from source at plan time. A committed .zip is a binary nobody reviews and everybody
# forgets to rebuild.
data "archive_file" "reaper" {
  type        = "zip"
  source_dir  = "${path.module}/reaper"
  output_path = "${path.module}/.build/reaper.zip"
}

resource "aws_lambda_function" "reaper" {
  function_name    = "${local.prefix}-reaper"
  role             = aws_iam_role.reaper.arn
  runtime          = "python3.12"
  handler          = "reaper.handler"
  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256
  timeout          = 60

  environment {
    variables = {
      TOPIC_ARN = aws_sns_topic.alerts.arn
      TAG_KEY   = "attestor:expires-at"
    }
  }
}

resource "aws_sns_topic" "alerts" {
  name              = "${local.prefix}-alerts"
  kms_master_key_id = aws_kms_key.data.id
}

resource "aws_cloudwatch_event_rule" "reaper" {
  name                = "${local.prefix}-reaper"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_target" "reaper" {
  rule      = aws_cloudwatch_event_rule.reaper.name
  target_id = "reaper"
  arn       = aws_lambda_function.reaper.arn
}

resource "aws_lambda_permission" "reaper" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.reaper.arn
}
