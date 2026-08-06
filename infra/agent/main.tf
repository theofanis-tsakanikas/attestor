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

# ── Identity: one user pool per tenant ───────────────────────────────────────
#
# Per tenant rather than one pool with a tenant attribute. A shared pool makes tenant
# membership a claim the pool asserts about a user, and every isolation bug in that shape
# starts with somebody editing an attribute. Separate pools make it a fact about which issuer
# signed the token.
#
# `lumen` is not here: it authenticates against an external OIDC provider, which is the point
# of having it. See tenants/lumen.yaml.
#
# The consequence of one pool per tenant is that there is one *issuer* per tenant, and a JWT
# authorizer validates against one issuer's keys. So there is one authorizer per tenant too —
# see `awscc_bedrockagentcore_gateway` below. Pointing a single authorizer at one pool while
# listing every pool's app client in `allowed_clients` reads as multi-tenant and is not:
# `allowed_clients` is checked *after* the signature, and tokens from the other pools are
# signed by keys that authorizer has never seen. Two of the three tenants simply cannot log
# in, and the failure looks like a broken IdP rather than a misconfigured gateway.

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

data "aws_ssm_parameter" "knowledge" {
  for_each = toset([
    "collection_arn",
    "evidence_kb_id",
    "regulatory_kb_id",
    "guardrail_id",
    "guardrail_version",
  ])

  name = "/${var.project}/knowledge/${each.value}"
}

locals {
  foundation = { for key, param in data.aws_ssm_parameter.foundation : key => param.value }
  knowledge  = { for key, param in data.aws_ssm_parameter.knowledge : key => param.value }

  # An EU cross-region inference profile is the foundation-model id with a region prefix:
  # `eu.anthropic.claude-haiku-4-5-...` routes to `anthropic.claude-haiku-4-5-...`. Deriving
  # it rather than declaring it twice means the two cannot disagree, and a profile that is
  # not prefixed (a first-party id used directly) trims to itself.
  reasoning_foundation_model = trimprefix(trimprefix(var.reasoning_model, "eu."), "global.")
}

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
  #checkov:skip=CKV_AWS_355: Two actions require `*`. `glue:Get*` is scoped by Lake Formation
  #rather than by ARN, and Athena's own workgroup constraint is the effective boundary on what
  #can be read. Everything else in this document names a resource.
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
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${local.knowledge.evidence_kb_id}",
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${local.knowledge.regulatory_kb_id}",
        ]
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # Both ARNs, because invoking through a cross-region inference profile needs both.
        # `var.reasoning_model` is a *profile* id (`eu.anthropic....`), and the previous
        # policy spent it as if it were a foundation-model id — an ARN that matches nothing,
        # so every narrative would have failed with AccessDenied at run time while the
        # apply reported success. The profile grants the right to route; the foundation-model
        # grant is what the regions it routes into actually check.
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.reasoning_model}",
          "arn:aws:bedrock:*::foundation-model/${local.reasoning_foundation_model}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:guardrail/${local.knowledge.guardrail_id}"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${local.foundation.evidence_bucket}/*",
          "arn:aws:s3:::${local.foundation.reports_bucket}/*",
          "arn:aws:s3:::${local.foundation.lake_bucket}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = local.foundation.kms_key_arn
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
  #checkov:skip=CKV_AWS_116: A DLQ catches failed *async* invocations. Gateway calls this
  #synchronously and surfaces the failure to the caller, which is where it belongs.
  #checkov:skip=CKV_AWS_272: Code signing needs a signing profile whose lifecycle would
  #outlive the estate it signs for.
  #checkov:skip=CKV_AWS_173: The environment holds identifiers — a workgroup name, two
  #knowledge-base ids, a guardrail version. No secret is passed this way; credentials come
  #from the execution role and Secrets Manager.
  function_name    = "${var.project}-tools"
  role             = aws_iam_role.tools.arn
  runtime          = "python3.12"
  handler          = "handler.invoke"
  filename         = data.archive_file.tools.output_path
  source_code_hash = data.archive_file.tools.output_base64sha256
  timeout          = 60
  memory_size      = 1024
  # Bounded on purpose. A tool that can fan out without limit is a tool that can exhaust the
  # Bedrock quota for every tenant at once, and a per-tenant platform where one tenant can do
  # that has no isolation worth the name.
  reserved_concurrent_executions = 20

  vpc_config {
    subnet_ids         = split(",", local.foundation.private_subnet_ids)
    security_group_ids = [local.foundation.endpoint_security_group_id]
  }

  environment {
    variables = {
      ATTESTOR_WORKGROUP = var.project
      ATTESTOR_DATABASE  = "${var.project}_gold"
      # The handler resolves through Athena and needs somewhere to put its results. It was
      # missing, and `os.environ["ATTESTOR_ATHENA_OUTPUT"]` in `build_toolbox` is not
      # optional — every tool call would have raised a KeyError and returned a 500.
      ATTESTOR_ATHENA_OUTPUT = "s3://${local.foundation.lake_bucket}/athena-results/"
      ATTESTOR_EVIDENCE_KB   = local.knowledge.evidence_kb_id
      ATTESTOR_REGULATORY_KB = local.knowledge.regulatory_kb_id
      ATTESTOR_GUARDRAIL_ID  = local.knowledge.guardrail_id
      ATTESTOR_GUARDRAIL_VER = local.knowledge.guardrail_version
      # Which model drafts a narrative. Without it the narrative provider refuses to build,
      # every narrative datapoint abstains, and the report blocks.
      ATTESTOR_REASONING_MODEL = var.reasoning_model
      ATTESTOR_REPORTS_BUCKET  = local.foundation.reports_bucket
      OTEL_SERVICE_NAME        = "attestor-tools"
    }
  }

  tracing_config {
    mode = "Active"
  }
}

# ── Observability ────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "agent" {
  #checkov:skip=CKV_AWS_338: The estate lives for days. Retaining its logs for a year would
  #keep records about an agent that no longer exists, and the run records in `gold.report_run`
  #are the durable audit trail — not CloudWatch.
  name              = "/aws/${var.project}/agent"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.foundation.kms_key_arn
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
  alarm_actions       = [local.foundation.alerts_topic_arn]
}

# ── AgentCore ────────────────────────────────────────────────────────────────
#
# The six components, and what each is actually for here.
#
# `Gateway` turns the tool handlers into MCP tools. That is the piece with the clearest
# payback: six operations described by an OpenAPI document the code generates, fronted by a
# managed MCP server with JWT auth. Hand-writing that plumbing is weeks, and the weeks buy
# nothing an auditor cares about.
#
# `Policy Engine` runs the *same Cedar files* the offline authorizer parses. Not a
# translation of them, not a re-expression — `file()` reads `policy/cedar/*.cedar` and hands
# the statement to AWS. So `attestor policy verify` on a laptop and the deployed engine
# cannot drift, and the cross-check in `tests/policy/` is a check on both.
#
# `Runtime` hosts the container. Session isolation between concurrent invocations is a real
# property and a tedious one to build; buying it is sensible. It is *not* where the agent's
# judgement lives — that is the resolver, and it is tested without an account.
#
# `Identity` (workload identity) gives the agent a principal of its own for outbound calls,
# distinct from the human's session. `Memory` is per tenant by construction. `Observability`
# is the log group and metric filters above, with `tenant_id` on every record.

resource "awscc_bedrockagentcore_workload_identity" "agent" {
  name = "${var.project}_agent"
}

# The Cedar policies, deployed from the same files the offline evaluator parses.
resource "awscc_bedrockagentcore_policy_engine" "main" {
  # Underscores, because a policy engine name must match `^[A-Za-z][A-Za-z0-9_]*$` — no
  # hyphens, unlike every other AgentCore resource in this file. Memory and Runtime below
  # already spell their names that way for the same reason.
  name        = "${var.project}_policy_engine"
  description = "Cedar policies from policy/cedar/, evaluated before any tool executes."
}

resource "awscc_bedrockagentcore_policy" "cedar" {
  for_each = fileset("${path.root}/../../policy/cedar", "*.cedar")

  # The file name, unchanged. It used to be rewritten to hyphens, which is exactly the
  # wrong direction: AgentCore policy names take `^[A-Za-z][A-Za-z0-9_]*$`, so
  # `tenant_isolation.cedar` had to become `tenant-isolation` to be rejected.
  name             = trimsuffix(each.value, ".cedar")
  policy_engine_id = awscc_bedrockagentcore_policy_engine.main.policy_engine_id
  description      = "Deployed verbatim from policy/cedar/${each.value}"

  # ACTIVE, not LOG_ONLY. A policy engine in log-only mode is a record of the decisions it
  # would have made, and the whole argument for deciding authorization before execution is
  # that the decision has effect.
  enforcement_mode = "ACTIVE"
  # A policy with a validation finding does not deploy. The alternative is a policy that is
  # live and subtly not what it says, which is worse than no policy: it is a control people
  # believe in.
  validation_mode = "FAIL_ON_ANY_FINDINGS"

  definition = {
    cedar = {
      statement = file("${path.root}/../../policy/cedar/${each.value}")
    }
  }
}

# One gateway per tenant, because a JWT authorizer validates against one issuer, and one
# pool per tenant means one issuer per tenant. This is not duplication for its own sake: it
# is the same statement the Cognito pools make, carried through to the edge. A token minted
# for helios reaches the helios gateway or it reaches nothing.
resource "awscc_bedrockagentcore_gateway" "tenant" {
  for_each = aws_cognito_user_pool.tenant

  # Hyphens here, underscores three resources up. Gateway takes
  # `^([0-9a-zA-Z][-]?){1,100}$` and Policy Engine takes `^[A-Za-z][A-Za-z0-9_]*$`, and the
  # two rules exclude each other — there is no name that satisfies both. Hence the per-kind
  # table in `scripts/check_agentcore_names.py` rather than one house style.
  name        = "${var.project}-gateway-${each.key}"
  role_arn    = aws_iam_role.gateway.arn
  description = "Attestor tools as MCP for ${each.key}. Tenant comes from the token."

  authorizer_type = "CUSTOM_JWT"
  authorizer_configuration = {
    custom_jwt_authorizer = {
      discovery_url = "https://cognito-idp.${var.region}.amazonaws.com/${each.value.id}/.well-known/openid-configuration"
      # Exactly one client: this tenant's. Listing every tenant's app client against one
      # issuer's keys is what made the previous configuration look multi-tenant while
      # admitting one tenant and locking out the rest.
      allowed_clients = [aws_cognito_user_pool_client.tenant[each.key].id]
    }
  }

  # `protocol_type` is omitted deliberately: the provider models it as a JSON-typed
  # attribute, and `protocol_configuration.mcp` already declares the protocol. Setting both
  # would be two statements of one fact.
  protocol_configuration = {
    mcp = {
      # Semantic search over tool descriptions. The descriptions are first-class engineering
      # here: a model that picks `search_evidence` when it needed `resolve_datapoint` will
      # cite a document instead of stating a figure, and the provenance gate will stop it —
      # loudly, at the end, rather than cheaply, at the start.
      search_type        = "SEMANTIC"
      supported_versions = ["2025-06-18"]
      session_configuration = {
        session_timeout_in_seconds = 900
      }
    }
  }

  policy_engine_configuration = {
    arn  = awscc_bedrockagentcore_policy_engine.main.policy_engine_arn
    mode = "ENFORCE"
  }

  kms_key_arn = local.foundation.kms_key_arn

  # `exception_level` is left unset. Its only permitted value is DEBUG, which returns
  # internal detail to the caller — an excellent map of the system for whoever provoked the
  # error. The default is the quiet one, and quiet is what we want.
}

# ── The target: what the gateway actually fronts ─────────────────────────────
#
# Without this the estate stands up perfectly and the agent has *no tools*. A gateway with no
# target is a valid gateway; `terraform apply` succeeds, `gateway_url` is populated, and every
# MCP session lists an empty toolset. That is the worst shape a deployment bug can take — no
# error anywhere, and a system that appears to be running.
#
# It is a provisioner rather than a resource because neither `hashicorp/awscc` (1.95.0, the
# latest published version) nor `hashicorp/aws` (5.x) ships a gateway-target resource. The
# control-plane API exists; the Terraform coverage does not.
#
# Two things keep this from becoming the exception that swallows the rule. It stays inside
# `terraform apply` and inside the dependency graph, so a destroy removes it rather than
# orphaning it on a deleted gateway. And `scripts/check_gateway_target.py` fails CI the day a
# provider ships the resource — so this is removed by a red build, not by somebody
# remembering that it was meant to be temporary.
resource "null_resource" "gateway_target" {
  for_each = awscc_bedrockagentcore_gateway.tenant

  triggers = {
    gateway_id = each.value.gateway_identifier
    lambda_arn = aws_lambda_function.tools.arn
    region     = var.agent_region
    # The generated tool schema. A tool added to `SPECS` changes this digest, which re-runs
    # the attach — otherwise the Gateway would keep describing the previous toolset.
    schema = filesha256("${path.module}/tools.openapi.json")
    script = filesha256("${path.module}/gateway-target.sh")
  }

  provisioner "local-exec" {
    command = join(" ", [
      "bash ${path.module}/gateway-target.sh attach",
      self.triggers.gateway_id,
      self.triggers.region,
      self.triggers.lambda_arn,
      "${path.module}/tools.openapi.json",
    ])
  }

  provisioner "local-exec" {
    when    = destroy
    command = "bash ${path.module}/gateway-target.sh detach ${self.triggers.gateway_id} ${self.triggers.region}"
  }

  depends_on = [aws_lambda_permission.gateway]
}

# The gateway invokes the handler; the handler's own resource policy has to say so. An
# identity policy on the gateway role is not sufficient for a cross-service invoke, and the
# failure mode — every tool call returning an access error at run time, after a green apply —
# is exactly the kind this layer is supposed to make impossible.
resource "aws_lambda_permission" "gateway" {
  for_each = awscc_bedrockagentcore_gateway.tenant

  statement_id  = "AllowInvokeFromGateway-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tools.function_name
  principal     = "bedrock-agentcore.amazonaws.com"
  source_arn    = each.value.gateway_arn
}

# Per-tenant memory. Separate resources rather than one store with a tenant key: a shared
# store makes isolation a property of every write path, and there are more write paths than
# anybody remembers.
resource "awscc_bedrockagentcore_memory" "tenant" {
  for_each = toset(var.cognito_tenants)

  name                  = "${var.project}_${each.value}"
  description           = "Per-tenant agent memory for ${each.value}."
  event_expiry_duration = var.memory_retention_days
  encryption_key_arn    = local.foundation.kms_key_arn
}

# ── Runtime ──────────────────────────────────────────────────────────────────

resource "aws_ecr_repository" "agent" {
  #checkov:skip=CKV_AWS_51: Tags are already immutable; this is the same setting read twice.
  name                 = "${var.project}-agent"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = local.foundation.kms_key_arn
  }
}

resource "awscc_bedrockagentcore_runtime" "agent" {
  count = var.deploy_runtime ? 1 : 0

  agent_runtime_name = replace("${var.project}_agent", "-", "_")
  description        = "Hosts the Attestor agent container. Judgement lives in the library."
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact = {
    container_configuration = {
      container_uri = "${aws_ecr_repository.agent.repository_url}:${var.agent_image_tag}"
    }
  }

  # The container reaches Athena, Bedrock and OpenSearch through the VPC endpoints in the
  # foundation layer. PUBLIC would work and would put the agent's egress outside the audited
  # path for no gain.
  network_configuration = {
    network_mode = "VPC"
    network_mode_config = {
      subnets         = split(",", local.foundation.private_subnet_ids)
      security_groups = [local.foundation.endpoint_security_group_id]
    }
  }

  protocol_configuration = "HTTP"

  authorizer_configuration = {
    custom_jwt_authorizer = {
      discovery_url = "https://cognito-idp.${var.region}.amazonaws.com/${values(aws_cognito_user_pool.tenant)[0].id}/.well-known/openid-configuration"
      allowed_clients = [
        for client in aws_cognito_user_pool_client.tenant : client.id
      ]
    }
  }

  environment_variables = {
    ATTESTOR_ROOT            = "/app"
    ATTESTOR_WORKGROUP       = var.project
    ATTESTOR_DATABASE        = "${var.project}_gold"
    ATTESTOR_ATHENA_OUTPUT   = "s3://${local.foundation.lake_bucket}/athena-results/"
    ATTESTOR_EVIDENCE_KB     = local.knowledge.evidence_kb_id
    ATTESTOR_REGULATORY_KB   = local.knowledge.regulatory_kb_id
    ATTESTOR_GUARDRAIL_ID    = local.knowledge.guardrail_id
    ATTESTOR_GUARDRAIL_VER   = local.knowledge.guardrail_version
    ATTESTOR_REASONING_MODEL = var.reasoning_model
    OTEL_SERVICE_NAME        = "attestor-agent"
  }

  tags = {
    "attestor:expires-at" = var.expires_at
  }
}

resource "awscc_bedrockagentcore_runtime_endpoint" "live" {
  count = var.deploy_runtime ? 1 : 0

  name             = "live"
  agent_runtime_id = awscc_bedrockagentcore_runtime.agent[0].agent_runtime_id
  description      = "The alias callers address. Pointing at a version, never at latest."
}

# ── Roles ────────────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "agentcore_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.project}-gateway"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
}

# The Gateway's only job is to reach the tool handlers. It cannot read the lake, invoke a
# model or touch a knowledge base — everything it fronts already has its own role, and a
# gateway that could do the work of its targets would be a way around them.
resource "aws_iam_role_policy" "gateway" {
  name = "invoke-tool-handlers"
  role = aws_iam_role.gateway.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.tools.arn
      }
    ]
  })
}

resource "aws_iam_role" "runtime" {
  name               = "${var.project}-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
}

resource "aws_iam_role_policy" "runtime" {
  #checkov:skip=CKV_AWS_355: `ecr:GetAuthorizationToken` and `glue:Get*` have no resource form.
  #The image pull is constrained by the repository ARN in the statement below it.
  name = "run-the-agent"
  role = aws_iam_role.runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.agent.arn
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # Both ARNs, because invoking through a cross-region inference profile needs both.
        # `var.reasoning_model` is a *profile* id (`eu.anthropic....`), and the previous
        # policy spent it as if it were a foundation-model id — an ARN that matches nothing,
        # so every narrative would have failed with AccessDenied at run time while the
        # apply reported success. The profile grants the right to route; the foundation-model
        # grant is what the regions it routes into actually check.
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.reasoning_model}",
          "arn:aws:bedrock:*::foundation-model/${local.reasoning_foundation_model}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:ApplyGuardrail"]
        Resource = "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:guardrail/${local.knowledge.guardrail_id}"
      },
      {
        Effect = "Allow"
        Action = ["bedrock:Retrieve"]
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${local.knowledge.evidence_kb_id}",
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/${local.knowledge.regulatory_kb_id}",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"]
        Resource = "arn:aws:athena:${var.region}:${data.aws_caller_identity.current.account_id}:workgroup/${var.project}"
      },
      {
        Effect   = "Allow"
        Action   = ["glue:GetTable", "glue:GetTables", "glue:GetDatabase", "glue:GetPartitions"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::${local.foundation.lake_bucket}",
          "arn:aws:s3:::${local.foundation.lake_bucket}/*",
          "arn:aws:s3:::${local.foundation.evidence_bucket}/*",
          "arn:aws:s3:::${local.foundation.reports_bucket}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = local.foundation.kms_key_arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"
      },
    ]
  })
}
