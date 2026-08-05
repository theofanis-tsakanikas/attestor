# Bootstrap — the one layer that is applied from a laptop.
#
# It exists because of a chicken-and-egg problem that has exactly one honest resolution: CI
# authenticates by assuming a role, and that role has to exist before CI can run. Something
# outside CI must create it once. Pretending otherwise means either a long-lived access key
# in a secret store — which this repository does not have and will not get — or a manual
# console click nobody records.
#
# So: apply this once, locally, with SSO credentials, and never again. Everything else in
# infra/ is applied only by a gated workflow, and `.github/workflows/ci.yml` fails a pull
# request that changes any other layer's backend configuration.
#
# What it creates is deliberately small and deliberately permanent: state, locking, and the
# identity CI uses. The destroy workflow does not touch it.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "attestor:layer"   = "bootstrap"
      "attestor:managed" = "terraform"
      # No expires-at. This layer outlives every estate, which is the point of it.
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  state_bucket = "${var.project}-tfstate-${data.aws_caller_identity.current.account_id}"
}

# ── State ────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_kms_key" "state" {
  description             = "${var.project} terraform state"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "state" {
  name          = "alias/${var.project}-tfstate"
  target_key_id = aws_kms_key.state.key_id
}

resource "aws_dynamodb_table" "locks" {
  name         = "${var.project}-tfstate-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

# ── The identity CI assumes ──────────────────────────────────────────────────

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.github_oidc_thumbprints
}

data "aws_iam_policy_document" "assume_from_github" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Scoped to this repository *and* to the environments that gate deployment. A wildcard
    # on `sub` would let any workflow in any repository this provider trusts assume the role,
    # which is the single most common way an OIDC setup ends up worse than a static key.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        for environment in var.deploy_environments :
        "repo:${var.github_repository}:environment:${environment}"
      ]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.project}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.assume_from_github.json
  max_session_duration = 3600
}

# Deliberately broad for a project whose whole estate is created and destroyed by this role,
# and deliberately bounded by two things that matter more than a narrower action list: the
# role can only be assumed from a gated environment in one repository, and the budget action
# below detaches this policy when spend crosses its threshold.
resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy" "deploy_iam" {
  name = "manage-service-roles"
  role = aws_iam_role.deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:*Role*", "iam:*Policy*", "iam:PassRole", "iam:TagRole"]
        Resource = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project}-*"
      },
      {
        Effect   = "Allow"
        Action   = ["iam:CreateServiceLinkedRole"]
        Resource = "*"
      }
    ]
  })
}

# ── The cost guard ───────────────────────────────────────────────────────────

resource "aws_budgets_budget" "estate" {
  name         = "${var.project}-estate"
  budget_type  = "COST"
  limit_amount = var.budget_eur
  limit_unit   = "EUR"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 60
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 85
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

# At the ceiling the deploy role loses its permissions. Not an email — an action. An alert
# that arrives while nobody is reading it has never stopped a bill.
resource "aws_budgets_budget_action" "disable_deploy" {
  budget_name        = aws_budgets_budget.estate.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.budget_action.arn

  action_threshold {
    action_threshold_type  = "PERCENTAGE"
    action_threshold_value = 100
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.deny_everything.arn
      roles      = [aws_iam_role.deploy.name]
    }
  }

  subscriber {
    address           = var.budget_alert_email
    subscription_type = "EMAIL"
  }
}

resource "aws_iam_policy" "deny_everything" {
  name = "${var.project}-budget-stop"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Deny"
        NotAction = ["iam:*", "sts:*", "s3:*", "dynamodb:*", "budgets:*"]
        Resource  = "*"
      }
    ]
  })
}

data "aws_iam_policy_document" "budget_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "budget_action" {
  name               = "${var.project}-budget-action"
  assume_role_policy = data.aws_iam_policy_document.budget_assume.json
}

resource "aws_iam_role_policy" "budget_action" {
  name = "attach-stop-policy"
  role = aws_iam_role.budget_action.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource = aws_iam_role.deploy.arn
      }
    ]
  })
}
