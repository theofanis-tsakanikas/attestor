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
  policy                  = data.aws_iam_policy_document.data_key.json
}

# An explicit policy rather than the implicit default. The default grants the whole account
# access through IAM, which makes "who can decrypt the evidence" a question about every policy
# in the account instead of a question about this document.
data "aws_iam_policy_document" "data_key" {
  #checkov:skip=CKV_AWS_109: A KMS key policy must grant `kms:*` to the account root, or the
  #key becomes unmanageable — AWS documents this as the one statement every key policy needs.
  #checkov:skip=CKV_AWS_111: Same statement. Constraining the root grant is what locks a key.
  #checkov:skip=CKV_AWS_356: `Resource = "*"` inside a *key* policy means "this key". There is
  #no other resource a key policy can name.
  statement {
    sid       = "AccountRoot"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid    = "ServicesThatEncryptOnOurBehalf"
    effect = "Allow"
    actions = [
      "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
      "kms:GenerateDataKey*", "kms:DescribeKey",
    ]
    resources = ["*"]
    principals {
      type = "Service"
      identifiers = [
        "logs.${var.region}.amazonaws.com",
        "s3.amazonaws.com",
        "sns.amazonaws.com",
        "athena.amazonaws.com",
        # AgentCore Memory encrypts a tenant's conversation history with this key. Unlike the
        # gateway — which encrypts under the role we hand it, and is therefore covered by an
        # identity policy — Memory holds data across sessions on its own, so the key has to
        # name the service. The alternative is an AWS-owned key, which would put a tenant's
        # transcripts outside the one key this estate's destroy actually deletes.
        "bedrock-agentcore.amazonaws.com",
      ]
    }
  }
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

# Terraform does not create the default security group, so leaving it alone leaves an
# unmanaged group that permits all traffic within the VPC. Adopting it with no rules is the
# only way to close it.
resource "aws_default_security_group" "locked" {
  vpc_id = aws_vpc.main.id
}

# Flow logs. The whole topology exists so egress has one audited path; not recording what
# went through it would make that an assertion rather than an observation.
resource "aws_flow_log" "vpc" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  iam_role_arn         = aws_iam_role.flow_logs.arn
  log_destination      = aws_cloudwatch_log_group.flow_logs.arn
  log_destination_type = "cloud-watch-logs"
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  #checkov:skip=CKV_AWS_338: The estate lives for days by design; a year of retention would
  #outlive the VPC by an order of magnitude and bill for logs about a network that is gone.
  name              = "/aws/${local.prefix}/vpc-flow-logs"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.prefix}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
}

resource "aws_iam_role_policy" "flow_logs" {
  name = "write-flow-logs"
  role = aws_iam_role.flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow_logs.arn}:*"
    }]
  })
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

  egress {
    description     = "S3 through the gateway endpoint, by prefix list"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [aws_vpc_endpoint.s3.prefix_list_id]
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
    # AgentCore's *data* plane, which is how the tool handler writes an analyst's question to
    # its tenant's memory. Without it that call has nowhere to go: this security group's egress
    # is the VPC and nothing else, so a service with no endpoint does not fail — it hangs, with
    # no packet refused and no error raised, until the Lambda's timeout kills it.
    #
    # That is exactly what happened. The tool resolved its answer in seconds and then sat for
    # the full 180s inside `create_event`, and the caller was told "An internal error occurred"
    # about a call that had already succeeded. Fail-open is only fail-open when the failure
    # arrives; a hang is neither open nor closed.
    "bedrock-agentcore",
    # ECR, both halves, or the AgentCore Runtime cannot fetch the image it is made of. The
    # control plane reported the runtime `READY` — which means the resource exists, not that
    # anything runs — while its logs said, every few seconds, `failed to resolve image ...
    # dial tcp 3.121.190.14:443: i/o timeout`. It had never once started, on any deploy, and
    # nothing noticed because nothing called it.
    #
    # `ecr.api` authenticates and `ecr.dkr` serves the layers; the manifest and blobs come from
    # S3, which already has a gateway endpoint. All three are needed and none of them was
    # reachable from a subnet whose egress is the VPC and nothing else.
    "ecr.api",
    "ecr.dkr",
  ])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
}

# A gateway endpoint is a route, not an interface, and a route is not a permission. Traffic to
# S3 still leaves through this security group and is still matched against its egress rules —
# and those allowed the VPC CIDR and nothing else, so every packet to an S3 address was dropped.
#
# Nothing noticed for a long time because nothing in this VPC talks to S3 directly. Athena is
# asked for its results through its own API and fetches the data server-side; the Lambda never
# opens an S3 client. The one thing that does is the container runtime pulling image layers,
# which come from an AWS-owned bucket — and it reported `dial tcp 52.219.170.178:443: i/o
# timeout` while ECR itself, reached through an interface endpoint, authenticated perfectly.
#
# The prefix list keeps this narrow: S3's published ranges in this region, not `0.0.0.0/0`.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
}

# ── Buckets ──────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "lake" {
  #checkov:skip=CKV_AWS_144: Cross-region replication for an estate measured in days would
  #copy the data somewhere the destroy workflow does not reach.
  #checkov:skip=CKV2_AWS_62: Event notifications need a consumer. Nothing here reacts to an
  #object landing; the pipeline is driven by the deploy workflow, on purpose.
  bucket        = "${local.prefix}-lake-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # the estate is ephemeral by design; teardown must not need a human
}

resource "aws_s3_bucket" "evidence" {
  #checkov:skip=CKV_AWS_144: See the lake bucket — the estate is ephemeral by design.
  #checkov:skip=CKV2_AWS_62: Nothing subscribes; ingestion is a deliberate workflow step.
  bucket        = "${local.prefix}-evidence-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "reports" {
  #checkov:skip=CKV_AWS_144: See the lake bucket — the estate is ephemeral by design.
  #checkov:skip=CKV2_AWS_62: Nothing subscribes; publication is a deliberate workflow step.
  bucket        = "${local.prefix}-reports-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

# Versioning everywhere the estate keeps something an auditor might re-read.
#
# Evidence is the obvious one: overwriting a document in place would make a lineage record
# point at bytes that no longer exist. Reports and the lake are versioned for the same
# reason one step removed — a figure is only re-derivable if the rows behind it still are.
resource "aws_s3_bucket_versioning" "data" {
  for_each = {
    lake     = aws_s3_bucket.lake.id
    evidence = aws_s3_bucket.evidence.id
    reports  = aws_s3_bucket.reports.id
  }

  bucket = each.value
  versioning_configuration {
    status = "Enabled"
  }
}

# Access logging on the evidence bucket. "Who read a tenant's documents" is a question that
# gets asked once, urgently, and cannot be answered retroactively.
resource "aws_s3_bucket" "access_logs" {
  #checkov:skip=CKV_AWS_145: S3 server access logging cannot write into a bucket encrypted
  #with a customer-managed key. AES256 is the only option that keeps the logs, and losing the
  #logs to gain a key we do not need is the wrong trade.
  #checkov:skip=CKV2_AWS_62: A log bucket that notifies on every write notifies constantly.
  #checkov:skip=CKV_AWS_18: A log bucket that logs itself is a recursion, not a control.
  #checkov:skip=CKV_AWS_21: Log objects are append-only by nature; versions of them are noise.
  #checkov:skip=CKV_AWS_144: The estate is ephemeral; replicating its logs outlives it.
  bucket        = "${local.prefix}-access-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id
  rule {
    apply_server_side_encryption_by_default {
      # AES256, not the CMK: S3 server access logging cannot write to a bucket encrypted with
      # a customer-managed key. Documented rather than worked around.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  #checkov:skip=CKV_AWS_300: There are no multipart uploads to a server-access-log bucket;
  #S3 writes those objects itself.
  bucket = aws_s3_bucket.access_logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

resource "aws_s3_bucket_logging" "data" {
  for_each = {
    lake     = aws_s3_bucket.lake.id
    evidence = aws_s3_bucket.evidence.id
    reports  = aws_s3_bucket.reports.id
  }

  bucket        = each.value
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "${each.key}/"
}

# The estate is ephemeral, so its data is too. An expiry that matches the estate's life is
# the honest lifecycle: keeping objects after the bucket's reason to exist is gone is how a
# "temporary" project becomes a standing data holding.
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  for_each = {
    lake     = aws_s3_bucket.lake.id
    evidence = aws_s3_bucket.evidence.id
    reports  = aws_s3_bucket.reports.id
  }

  bucket = each.value

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
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
  #checkov:skip=CKV_AWS_355: `tag:GetResources` has no resource form; the tagging API is
  #account-scoped by design. Note what is *not* here: no delete verb of any kind.
  #checkov:skip=CKV_AWS_290: The only write is `sns:Publish`. The reaper reports; it never
  #removes anything, which is the decision recorded beside the resource.
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
  #checkov:skip=CKV_AWS_117: It reads tags and publishes to SNS. Putting it in the VPC would
  #add an ENI and a NAT dependency to a function whose whole job is to work when things are
  #broken.
  #checkov:skip=CKV_AWS_116: A DLQ catches failed *async* invocations. This one runs on a
  #schedule; a missed sweep is picked up six hours later by the next.
  #checkov:skip=CKV_AWS_272: Code signing needs a signing profile and a key this estate would
  #create and destroy alongside the function it signs, which signs nothing meaningful.
  function_name    = "${local.prefix}-reaper"
  role             = aws_iam_role.reaper.arn
  runtime          = "python3.12"
  handler          = "reaper.handler"
  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256
  timeout          = 60
  # One at a time. A sweep that overlaps itself sends the same alert twice, and an alert that
  # arrives twice is an alert people start filtering.
  reserved_concurrent_executions = 1
  kms_key_arn                    = aws_kms_key.data.arn

  tracing_config {
    mode = "Active"
  }

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

# ── What this layer offers its neighbours ────────────────────────────────────
#
# Published as SSM parameters, not read out of this layer's state file.
#
# `terraform_remote_state` was the previous mechanism and it is the wrong one for a reason
# the repository's own rules already state: it hands the reading layer the *entire* state —
# every resource, every attribute, and anything sensitive that happens to be in there — and
# it couples two layers to a storage format instead of to a contract. It also requires every
# consumer to hold read access to the state bucket, which is the blast radius of a bug in the
# smallest layer becoming the blast radius of the largest.
#
# What a layer offers its neighbours should be a short, deliberate, enumerable list. This is
# that list; adding to it is a visible change.
locals {
  published = {
    vpc_id                     = aws_vpc.main.id
    private_subnet_ids         = join(",", aws_subnet.private[*].id)
    endpoint_security_group_id = aws_security_group.endpoints.id
    kms_key_arn                = aws_kms_key.data.arn
    lake_bucket                = aws_s3_bucket.lake.id
    evidence_bucket            = aws_s3_bucket.evidence.id
    reports_bucket             = aws_s3_bucket.reports.id
    alerts_topic_arn           = aws_sns_topic.alerts.arn
  }
}

resource "aws_ssm_parameter" "published" {
  #checkov:skip=CKV2_AWS_34: These are identifiers — a VPC id, three bucket names, two ARNs.
  #None is a secret, and a SecureString would add a KMS grant to every consuming role in
  #exchange for encrypting facts that are already visible in the console to anyone who can
  #read them here.
  #checkov:skip=CKV_AWS_337: Same reason. Secrets live in Secrets Manager; this is a
  #cross-layer reference table.
  for_each = local.published

  name        = "/${var.project}/foundation/${each.key}"
  description = "Cross-layer reference published by infra/foundation."
  type        = "String"
  value       = each.value
  tier        = "Standard"
}
