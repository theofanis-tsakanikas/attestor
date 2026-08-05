# Agent — AgentCore Runtime, Gateway, Identity, Memory and Observability.
#
# One idea runs through the whole layer: **AgentCore is a runtime, not the business logic.**
# The agent's behaviour — which datapoint to resolve, what a narrative may say, when to
# abstain — lives in this repository's Python and is fully tested without an account. What
# Gateway, Identity and Memory provide is session isolation, an authenticated principal, and
# per-tenant state. That split is deliberate: it keeps the choice of AgentCore a two-way door.
# If it stops fitting, the tools are plain handlers behind an OpenAPI description and they
# move.
#
# A note on region. AgentCore is not available everywhere, and the data plane in this project
# is deliberately European. If the agent plane has to sit elsewhere, that is a documented
# split with a data-residency consequence — see docs/DAY-ONE.md — and not something to
# discover during a demo.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws   = { source = "hashicorp/aws", version = "~> 5.70" }
    awscc = { source = "hashicorp/awscc", version = "~> 1.20" }
  }
  backend "s3" { key = "agent/terraform.tfstate" }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "attestor:layer"      = "agent"
      "attestor:managed"    = "terraform"
      "attestor:expires-at" = var.expires_at
    }
  }
}

provider "awscc" {
  region = var.agent_region
}

data "aws_caller_identity" "current" {}

data "terraform_remote_state" "foundation" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "foundation/terraform.tfstate"
    region = var.region
  }
}

data "terraform_remote_state" "knowledge" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "knowledge/terraform.tfstate"
    region = var.region
  }
}

# ── Identity: one user pool per tenant ───────────────────────────────────────
#
# Per tenant rather than one pool with a tenant attribute. A shared pool makes tenant
# membership a claim the pool asserts about a user, and every isolation bug in that shape
# starts with somebody editing an attribute. Separate pools make it a fact about which issuer
# signed the token.
#
# `lumen` is not here: it authenticates against an external OIDC provider, which is the point
# of having it. See tenants/lumen.yaml.

resource "aws_cognito_user_pool" "tenant" {
  for_each = toset(var.cognito_tenants)

  name                     = "${var.project}-${each.value}"
  deletion_protection      = "INACTIVE"
  mfa_configuration        = "OPTIONAL"
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 1
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "roles" {
  for_each = {
    for pair in setproduct(var.cognito_tenants, var.role_groups) :
    "${pair[0]}-${pair[1]}" => { tenant = pair[0], group = pair[1] }
  }

  name         = "${each.value.tenant}-${each.value.group}"
  user_pool_id = aws_cognito_user_pool.tenant[each.value.tenant].id
  description  = "Maps to a role in tenants/${each.value.tenant}.yaml"
}

resource "aws_cognito_user_pool_client" "tenant" {
  for_each = aws_cognito_user_pool.tenant

  name                                 = "${var.project}-${each.key}"
  user_pool_id                         = each.value.id
  generate_secret                      = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  allowed_oauth_flows_user_pool_client = true
  callback_urls                        = var.callback_urls
  supported_identity_providers         = ["COGNITO"]

  # Short access tokens. A session carries authority over a tenant's reporting data; an
  # eight-hour token turns a stolen browser into an eight-hour engagement.
  access_token_validity  = 15
  id_token_validity      = 15
  refresh_token_validity = 8
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}

# ── The tool handlers behind Gateway ─────────────────────────────────────────

data "aws_iam_policy_document" "tools_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tools" {
  name               = "${var.project}-tools"
  assume_role_policy = data.aws_iam_policy_document.tools_assume.json
}

resource "aws_iam_role_policy" "tools" {
  name = "resolve-and-retrieve"
  role = aws_iam_role.tools.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"]
        # Only through the workgroup, whose scan ceiling and result location are enforced
        # server-side. A query outside it would bypass both.
        Resource = "arn:aws:athena:${var.region}:${data.aws_caller_identity.current.account_id}:workgroup/${var.project}"
      },
      {
        Effect   = "Allow"
        Action   = ["glue:GetTable", "glue:GetTables", "glue:GetDatabase", "glue:GetPartitions"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"]
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${data.terraform_remote_state.knowledge.outputs.evidence_kb_id}",
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${data.terraform_remote_state.knowledge.outputs.regulatory_kb_id}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "arn:aws:bedrock:*::foundation-model/${var.reasoning_model}"
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:guardrail/${data.terraform_remote_state.knowledge.outputs.guardrail_id}"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${data.terraform_remote_state.foundation.outputs.evidence_bucket}/*",
          "arn:aws:s3:::${data.terraform_remote_state.foundation.outputs.reports_bucket}/*",
          "arn:aws:s3:::${data.terraform_remote_state.foundation.outputs.lake_bucket}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = data.terraform_remote_state.foundation.outputs.kms_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}

# Note what this role cannot do: it has no `bedrock:CreateGuardrail`, no `iam:*`, and no
# write access to `overrides/`. An agent cannot loosen its own guardrail, widen its own
# permissions, or sign an acceptance — which is the same statement Cedar makes, made again
# one layer down where a policy bug cannot reach.

# `make package` vendors the attestor package and its dependencies into this directory
# before a deploy. The zip is built here rather than committed: a committed artefact drifts
# from the source it was built from, and the drift is invisible.
data "archive_file" "tools" {
  type        = "zip"
  source_dir  = "${path.module}/tools"
  output_path = "${path.module}/.build/tools.zip"
}

resource "aws_lambda_function" "tools" {
  function_name    = "${var.project}-tools"
  role             = aws_iam_role.tools.arn
  runtime          = "python3.12"
  handler          = "handler.invoke"
  filename         = data.archive_file.tools.output_path
  source_code_hash = data.archive_file.tools.output_base64sha256
  timeout          = 60
  memory_size      = 1024

  vpc_config {
    subnet_ids         = data.terraform_remote_state.foundation.outputs.private_subnet_ids
    security_group_ids = [data.terraform_remote_state.foundation.outputs.endpoint_security_group_id]
  }

  environment {
    variables = {
      ATTESTOR_WORKGROUP      = var.project
      ATTESTOR_EVIDENCE_KB    = data.terraform_remote_state.knowledge.outputs.evidence_kb_id
      ATTESTOR_REGULATORY_KB  = data.terraform_remote_state.knowledge.outputs.regulatory_kb_id
      ATTESTOR_GUARDRAIL_ID   = data.terraform_remote_state.knowledge.outputs.guardrail_id
      ATTESTOR_GUARDRAIL_VER  = data.terraform_remote_state.knowledge.outputs.guardrail_version
      ATTESTOR_REPORTS_BUCKET = data.terraform_remote_state.foundation.outputs.reports_bucket
      OTEL_SERVICE_NAME       = "attestor-tools"
    }
  }

  tracing_config {
    mode = "Active"
  }
}

# ── Observability ────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/${var.project}/agent"
  retention_in_days = var.log_retention_days
  kms_key_id        = data.terraform_remote_state.foundation.outputs.kms_key_arn
}

# Every span carries tenant_id and session_id. Without them a latency graph is one line for
# three customers, and the per-tenant cost meter has nothing to attribute a charge to.
resource "aws_cloudwatch_log_metric_filter" "denied" {
  name           = "${var.project}-authorization-denied"
  log_group_name = aws_cloudwatch_log_group.agent.name
  pattern        = "{ $.event = \"authorization.denied\" }"

  metric_transformation {
    name      = "AuthorizationDenied"
    namespace = "Attestor"
    value     = "1"
    dimensions = {
      tenant = "$.tenant"
      action = "$.action"
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "injection" {
  name           = "${var.project}-injection-flagged"
  log_group_name = aws_cloudwatch_log_group.agent.name
  pattern        = "{ $.event = \"injection.flagged\" }"

  metric_transformation {
    name      = "InjectionFlagged"
    namespace = "Attestor"
    value     = "1"
    dimensions = {
      tenant = "$.tenant"
      rule   = "$.rule"
    }
  }
}

# A denial is normal; a *spike* in denials is somebody probing. The alarm is on the rate, not
# on the event, because alerting on every denial trains people to close the tab.
resource "aws_cloudwatch_metric_alarm" "denial_spike" {
  alarm_name          = "${var.project}-denial-spike"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "AuthorizationDenied"
  namespace           = "Attestor"
  period              = 300
  statistic           = "Sum"
  threshold           = var.denial_alarm_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [data.terraform_remote_state.foundation.outputs.alerts_topic_arn]
}
