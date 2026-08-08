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
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
    # Pinned to the minor, not floated. awscc 1.96.0 began writing `protocol_type` on the
    # gateway, which turns every subsequent apply into an UpdateResource — and Cloud Control
    # answers an update by sending the whole authorizer back with `AllowedAudience: []`, which
    # the model rejects. Two deploys died there, and neither had anything to do with the change
    # being deployed: a `terraform init -upgrade` run for an unrelated provider moved it.
    awscc  = { source = "hashicorp/awscc", version = "~> 1.95.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
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

  # `OPTIONAL` on its own is rejected: Cognito requires at least one second factor to be
  # enabled before it will let anyone opt in to one. Software tokens rather than SMS —
  # an authenticator app costs nothing per user, works without a phone number on file, and
  # is not SIM-swappable, which for people who sign off a regulated disclosure is the point.
  software_token_mfa_configuration {
    enabled = true
  }

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

# The issuer each tenant's tokens must carry, keyed the way the code reads it. This is the
# only place the real value exists: it contains a pool id Terraform generates, so
# `tenants/*.yaml` can only hold a placeholder.
#
# Without this the committed placeholder is what `Session._check_provider` compares a token's
# `iss` against, and no real token matches — which is exactly what was deployed, for as long
# as nothing ever called the gateway to find out.
locals {
  tenant_issuers = {
    for tenant, pool in aws_cognito_user_pool.tenant :
    "ATTESTOR_ISSUER_${upper(tenant)}" => "https://cognito-idp.${var.region}.amazonaws.com/${pool.id}"
  }

  # Both of this tenant's app clients: the one its people sign in through and the one the
  # deploy authenticates as to prove the boundary. A Cognito token carries the client *id* in
  # `aud`, so `tenants/*.yaml` cannot name it; and one undertaking having two applications is
  # ordinary, which is why the check accepts a set rather than a single value.
  tenant_audiences = {
    for tenant, pool in aws_cognito_user_pool.tenant :
    "ATTESTOR_AUDIENCE_${upper(tenant)}" => aws_cognito_user_pool_client.tenant[tenant].id
  }

  # The memory each tenant's events are written to. One resource per tenant rather than one
  # memory with a namespace column: a filter can be forgotten on a single call, a separate
  # resource cannot be. Empty until the memories exist, which is why it is merged rather than
  # indexed — `agent` is applied as one layer and the map is complete by then.
  tenant_memories = {
    for tenant, m in awscc_bedrockagentcore_memory.tenant :
    "ATTESTOR_MEMORY_${upper(tenant)}" => m.memory_id
  }

  # Which role a call arriving through each tenant's gateway carries. See `var.gateway_roles`:
  # the platform gives the handler a tenant and no claims, so this is declared rather than
  # inferred, and a change to it is a reviewed change.
  tenant_gateway_roles = {
    for tenant, role in var.gateway_roles :
    "ATTESTOR_GATEWAY_ROLE_${upper(tenant)}" => role
  }

  tenant_identity_env = merge(
    local.tenant_issuers,
    local.tenant_audiences,
    local.tenant_memories,
    local.tenant_gateway_roles,
  )
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

  # The code flow is how a person signs in and is unchanged. `ADMIN_USER_PASSWORD_AUTH` is how
  # the deploy signs in as the verification principal — through this same application, which
  # makes the token it gets the same shape a person's is, down to the `aud` claim the gateway
  # matches on. Admin-initiated, so it is reachable only by a caller that can already call the
  # Cognito control plane; it is not a password grant exposed to the internet.
  explicit_auth_flows = [
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

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

# ── A principal the deployment can actually authenticate as ──────────────────
#
# Everything above this comment was deployed, correct, and unreachable. There was no way to
# obtain a token for any tenant: no domain, no users, and an app client offering only the
# authorization-code flow. So the gateway, the runtime, the tool handlers and the Cedar
# `forbid` at the edge had never served a single request, and the first thing a real token
# would have hit was our own issuer check comparing it against `eu-central-1_EXAMPLE`.
#
# A user, not a machine-to-machine client, and the distinction is the whole value of the
# exercise. Only a user token carries `cognito:groups`, which is what `Session.from_claims`
# maps onto a role. A client-credentials token carries scopes instead, and verifying with one
# would mean verifying a different code path from the one a person uses — proving the parts we
# did not need to prove and skipping the part we did.

resource "aws_cognito_user_pool_domain" "tenant" {
  for_each = aws_cognito_user_pool.tenant

  domain       = "${var.project}-${each.key}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = each.value.id
}

# Never in the repository and never in a log. It exists so a workflow can prove the tenant
# boundary holds; it is not a way in for a person.
resource "random_password" "verification" {
  for_each = aws_cognito_user_pool.tenant

  # Without a keeper this value is generated once and kept forever, which would have made the
  # comment above it false. Keyed on the estate's expiry, which moves every time the layer is
  # stood up — so the credential's lifetime is the estate's lifetime, by construction rather
  # than by a rotation schedule nobody would run on an estate that lives for a day.
  keepers = {
    expires_at = var.expires_at
  }

  length           = 32
  special          = true
  override_special = "!@#%^*-_=+"
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 2
}

resource "aws_secretsmanager_secret" "verification" {
  #checkov:skip=CKV2_AWS_57: Rotation here would be a Lambda that changes a password nobody
  # holds for longer than the estate exists. `random_password.verification` is keyed on
  # `expires_at`, so the credential is replaced whenever this layer is applied, and the
  # estate is applied per deploy under a TTL the reaper enforces. A rotation schedule on top
  # of that is a second mechanism for the same guarantee, and the weaker one — it would keep
  # running against an estate that is meant to be gone.
  for_each = aws_cognito_user_pool.tenant

  name                    = "${var.project}/verification/${each.key}"
  description             = "Password for the ${each.key} verification principal. Rotated on every apply."
  kms_key_id              = local.foundation.kms_key_arn
  recovery_window_in_days = 0

  tags = {
    "attestor:expires-at" = var.expires_at
  }
}

resource "aws_secretsmanager_secret_version" "verification" {
  for_each = aws_cognito_user_pool.tenant

  secret_id = aws_secretsmanager_secret.verification[each.key].id
  secret_string = jsonencode({
    username = "${var.project}-verification"
    password = random_password.verification[each.key].result
    # The client has a secret, so `AdminInitiateAuth` requires SECRET_HASH — an HMAC over
    # username+client_id. `scripts/verify_agentcore.py` computes it; five lines there buys a
    # gateway authorizer that never has to change.
    client_id     = aws_cognito_user_pool_client.tenant[each.key].id
    client_secret = aws_cognito_user_pool_client.tenant[each.key].client_secret
    pool_id       = each.value.id
  })
}

resource "aws_cognito_user" "verification" {
  for_each = aws_cognito_user_pool.tenant

  user_pool_id = each.value.id
  username     = "${var.project}-verification"
  password     = random_password.verification[each.key].result

  # No email, no phone, no recovery path. It is not a person and must not be recoverable as
  # one; the only way to hold this credential is to be allowed to read the secret.
  message_action = "SUPPRESS"
}

# `preparers`, not `assurance`. A preparer may resolve a datapoint and read lineage, which is
# what the verification calls, and may not approve anything — so the principal used to test
# the boundary has no authority the test does not need.
resource "aws_cognito_user_in_group" "verification" {
  for_each = aws_cognito_user_pool.tenant

  user_pool_id = each.value.id
  group_name   = aws_cognito_user_group.roles["${each.key}-preparers"].name
  username     = aws_cognito_user.verification[each.key].username
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

# The handler runs in the private subnets, and Lambda refuses to create a function whose role
# cannot manage an ENI — `The provided execution role does not have permissions to call
# CreateNetworkInterface on EC2`, at create time, not at invoke time. AWS's own policy rather
# than a hand-written copy: those actions need `Resource = "*"` because an ENI has no ARN
# before it exists, so writing them out means writing a wildcard and arguing with a scanner.
resource "aws_iam_role_policy_attachment" "tools_vpc" {
  role       = aws_iam_role.tools.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
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
        # Writing an analyst's question to their own tenant's memory, and reading it back.
        # Scoped to the memories this layer creates: the module derives the namespace from a
        # verified session, and this makes that derivation the *only* reachable one — a code
        # path that tried to write elsewhere would be refused by IAM as well as by the code.
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
        ]
        Resource = [
          for m in awscc_bedrockagentcore_memory.tenant : m.memory_arn
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
        # `ListBucket` and `GetBucketLocation` are not padding. Athena resolves a table's
        # location and writes its result set before it runs anything, and without them
        # `StartQueryExecution` fails immediately — the tool answered in 809 ms with
        # `E_RESOLVER_ERROR`, far too fast for a query to have been attempted. The deploy role
        # is PowerUser, so the same code runs fine from a runner and only fails here.
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:AbortMultipartUpload",
          "s3:ListBucket",
          "s3:GetBucketLocation",
        ]
        Resource = [
          # Both forms: the bucket itself for the list and location calls, and its contents
          # for the object calls. A policy with only the second is why this failed.
          "arn:aws:s3:::${local.foundation.evidence_bucket}",
          "arn:aws:s3:::${local.foundation.evidence_bucket}/*",
          "arn:aws:s3:::${local.foundation.reports_bucket}",
          "arn:aws:s3:::${local.foundation.reports_bucket}/*",
          "arn:aws:s3:::${local.foundation.lake_bucket}",
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
  # 180, not 60. These tools query a lakehouse: `read_lineage` and `resolve_datapoint` run the
  # datapoint's SQL *and* its cross-check through Athena, and the first call after a deploy pays
  # a cold start on top. The live gateway returned "An internal error occurred" after exactly
  # 60000 ms — a Lambda timeout, surfaced to the caller as an internal error, which is the least
  # diagnosable shape a wrong limit can take.
  #
  # Bounded rather than generous: the MCP session allows 900s and Lambda allows more still. A
  # tool that cannot answer in three minutes is not slow, it is broken, and should say so.
  timeout     = 180
  memory_size = 1024
  # Bounded on purpose. A tool that can fan out without limit is a tool that can exhaust the
  # Bedrock quota for every tenant at once, and a per-tenant platform where one tenant can do
  # that has no isolation worth the name.
  reserved_concurrent_executions = 20

  vpc_config {
    subnet_ids         = split(",", local.foundation.private_subnet_ids)
    security_group_ids = [local.foundation.endpoint_security_group_id]
  }

  environment {
    variables = merge(local.tenant_identity_env, {
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
    })
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

# `policy/agentcore/`, not `policy/cedar/`. They are different policy languages wearing the
# same name, and the deploy is what established it.
#
# AgentCore's engine authorizes one question: may this OAuth principal invoke this tool at this
# gateway? Its entities are fixed — `AgentCore::OAuthUser`, `AgentCore::Action::"<target>___<tool>"`,
# `AgentCore::Gateway::"<arn>"` — the resource scope must be constrained to a gateway, and the
# only facts available are tags lifted off the token and the tool's own arguments.
#
# `policy/cedar/` answers a different question, about domain objects: may this role read this
# datapoint, for this tenant, in this period. Its resources carry a `tenant` attribute.
# AgentCore has no such entity, and it rejected every one of those policies for exactly that
# reason: "a wildcard resource was detected".
#
# Transplanting them would mean re-deriving the tenant from `principal.getTag(...)`, and this
# repository has not verified how Cognito's claims arrive as tags. Asserting that mapping in an
# authorization policy is the one place where nearly right and wrong are the same thing, so the
# deployed set is small, native, and certainly true: the gateway is the grant, and the override
# door stays shut. `policy/cedar/` keeps enforcing the rest, in the tool handler, where the
# whole isolation suite already exercises it.
locals {
  agentcore_policy_dir = "${path.root}/../../policy/agentcore"
  gateway_target_name  = "attestor-tools"

  # The actions the permit grants, built from the same file the gateway target is built from
  # so the two cannot disagree. `request_override` is left out on purpose: it is forbidden by
  # the policy beside this one, and excluding it here means default-deny holds it shut even if
  # that forbid were ever removed.
  permitted_tools = [
    for tool in jsondecode(file("${path.module}/tools.openapi.json")).tools :
    tool.name if tool.name != "request_override"
  ]
  permitted_actions = join(",\n        ", [
    for name in local.permitted_tools :
    "AgentCore::Action::\"${local.gateway_target_name}___${name}\""
  ])

  # One copy per gateway, because a policy names the gateway it applies to.
  agentcore_policies = merge([
    for tenant, gateway in awscc_bedrockagentcore_gateway.tenant : {
      for filename in fileset(local.agentcore_policy_dir, "*.cedar") :
      "${replace(trimsuffix(filename, ".cedar"), "-", "_")}_${tenant}" => templatefile(
        "${local.agentcore_policy_dir}/${filename}",
        {
          gateway_arn = gateway.gateway_arn
          target      = local.gateway_target_name
          actions     = local.permitted_actions
        }
      )
    }
  ]...)
}

resource "awscc_bedrockagentcore_policy" "cedar" {
  for_each = local.agentcore_policies

  # `<file>_<tenant>`, underscores because this resource takes `^[A-Za-z][A-Za-z0-9_]*$`.
  name             = each.key
  policy_engine_id = awscc_bedrockagentcore_policy_engine.main.policy_engine_id
  description      = "From policy/agentcore/, one policy per gateway"

  # `CreatePolicy` validates the action names in a statement against the gateway — an
  # operation AWS authorizes as `InvokeGateway`, on the gateway ARN. So the tools have to be
  # attached before a policy can name one: without this, `${target}___request_override` is an
  # action the gateway does not yet have, and the policy lands in CREATE_FAILED.
  depends_on = [null_resource.gateway_target]

  # ACTIVE, not LOG_ONLY. A policy engine in log-only mode is a record of the decisions it
  # would have made, and the whole argument for deciding authorization before execution is
  # that the decision has effect.
  enforcement_mode = "ACTIVE"
  # `IGNORE_ALL_FINDINGS`, and this is the one place in the repository where a scanner is
  # deliberately overruled, so the reasoning is here rather than in a commit message.
  #
  # The Cedar analyzer has two verdicts and no way to say "expected". It reported the permit as
  # *Overly Permissive* — it allows every listed tool at this gateway — and the forbid as
  # *Overly Restrictive*: "will deny every request for principal AgentCore::OAuthUser, action
  # attestor-tools___request_override". Both findings are correct, and both are the intent
  # restated. `FAIL_ON_ANY_FINDINGS` is the right mode for a policy that expresses a
  # condition; it cannot express a policy whose content is "always" or "never".
  #
  # What the analyzer would have caught is a permit that quietly widened, so that is now
  # checked here instead: the actions are enumerated from the tool schema rather than left
  # open, and `scripts/check_agentcore_policies.py` fails the build if the list drifts from it.
  validation_mode = "IGNORE_ALL_FINDINGS"

  definition = {
    cedar = {
      statement = each.value
    }
  }
}

# AgentCore assumes the gateway role *during* CreateGateway, to check it can reach the policy
# engine. Terraform sees the gateway reference `aws_iam_role.gateway` and not the policy
# attached to it, so it happily creates both at once — and the check ran against a role whose
# permissions arrived a second later. The error named `AuthorizeAction`, the permission was
# already in the file, and the second attempt failed identically.
#
# The sleep is not superstition. IAM is eventually consistent and this check is immediate, so
# ordering alone leaves a race whose loss costs a ten-minute deploy. Fifteen seconds, once, at
# the one point where the cost is asymmetric.
resource "null_resource" "gateway_role_settled" {
  triggers = {
    policy = aws_iam_role_policy.gateway.policy
  }

  provisioner "local-exec" {
    command = "sleep 15"
  }
}

# One gateway per tenant, because a JWT authorizer validates against one issuer, and one
# pool per tenant means one issuer per tenant. This is not duplication for its own sake: it
# is the same statement the Cognito pools make, carried through to the edge. A token minted
# for helios reaches the helios gateway or it reaches nothing.
resource "awscc_bedrockagentcore_gateway" "tenant" {
  for_each = aws_cognito_user_pool.tenant

  depends_on = [
    aws_iam_role_policy.gateway,
    null_resource.gateway_role_settled,
  ]

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
      # Exactly one client: this tenant's. Listing every tenant's app client against one
      # issuer's keys is what made the previous configuration look multi-tenant while
      # admitting one tenant and locking out the rest.
      #
      # It stays one for a second reason now. Adding a client here is an *update* to the
      # gateway, and Cloud Control sends the whole authorizer back — including
      # `AllowedAudience: []`, which the model rejects with "0 subschemas matched". So the
      # verification principal signs in through this same client, which is the more faithful
      # test anyway: it is the application a person uses.
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

  environment_variables = merge(local.tenant_identity_env, {
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
  })

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
      },
      {
        # The gateway reads the policy engine with *its own* role, at create time, before it
        # has served anything — `GenesisPolicyEngineCheck`. Without it the gateway does not
        # come up at all, which is the good version of this failure: the bad version is
        # documented, and it is a gateway that comes up and denies every tool call while the
        # permit policies sit there looking correct.
        Sid    = "PolicyEngineConfiguration"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetPolicyEngine"]
        # `GetPolicyEngine` alone is what turns a LOG_ONLY engine into a silent one, per AWS's
        # own troubleshooting note. It is here even though this engine is ACTIVE.
        Resource = [awscc_bedrockagentcore_policy_engine.main.policy_engine_arn]
      },
      {
        Sid    = "PolicyEngineAuthorization"
        Effect = "Allow"
        Action = [
          "bedrock-agentcore:AuthorizeAction",
          "bedrock-agentcore:PartiallyAuthorizeActions",
        ]
        Resource = [
          awscc_bedrockagentcore_policy_engine.main.policy_engine_arn,
          # Both actions need the *gateway* ARN as well as the engine's. It is a wildcard
          # rather than the gateways below, and not for convenience: the check runs while the
          # gateway is being created, so a policy naming the gateway could only exist after
          # the thing that needs it. Scoped to this account and region, on a role only
          # AgentCore can assume, for gateways only this layer creates.
          "arn:aws:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:gateway/*",
        ]
      },
      {
        # The gateway encrypts its target configuration with the estate's key, under its own
        # role, at create time — `GenesisMCPTargetTargetEncryption`. The tools role and the
        # runtime role were already granted this; the gateway was the one that had no reason
        # to touch a key until it turned out to store something.
        Sid      = "GatewayEncryption"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt", "kms:DescribeKey"]
        Resource = local.foundation.kms_key_arn
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
        # The runtime serves the same tools as the Lambda and records the same events, so it
        # needs the same grant. Two surfaces, one behaviour: a difference here would mean an
        # analyst's history depended on which door they came through.
        Action = [
          "bedrock-agentcore:CreateEvent",
          "bedrock-agentcore:ListEvents",
          "bedrock-agentcore:GetEvent",
        ]
        Resource = [
          for m in awscc_bedrockagentcore_memory.tenant : m.memory_arn
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
