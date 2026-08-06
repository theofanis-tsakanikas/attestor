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

  github_oidc_url  = "https://token.actions.githubusercontent.com"
  github_oidc_host = "token.actions.githubusercontent.com"

  # Derived, never transcribed. An OIDC provider's ARN is a function of the account and the
  # issuer host, so adopting one that already exists needs no value copied out of a console
  # into a variable — which is the step that later goes stale without anyone noticing.
  github_oidc_provider_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/${local.github_oidc_host}"
  )

  # GitHub issues an *immutable* subject: the owner and the repository each carry their
  # numeric id, `owner@1234/repo@5678`. Names can be released and re-registered by somebody
  # else; ids cannot, so a trust scoped to names can be inherited by whoever claims the name
  # after you delete the repository. That is the attack the format closes.
  #
  # Both forms are accepted because the account decides which it sends, not this file, and a
  # federation that works only against the format in use on the day it was written is a
  # federation that breaks on a Tuesday. Both are equally specific — one repository, one
  # environment — so accepting the pair widens nothing.
  #
  # Written out, four lines where a `for` over two lists would do. The loop moved the `repo:`
  # prefix behind two more locals, and the subject a trust policy grants is the last string
  # here that should have to be assembled in someone's head to be read.
  # `scripts/check_oidc_subjects.py` reads this list, and it should be reading what IAM gets.
  github_owner = split("/", var.github_repository)[0]
  github_repo  = split("/", var.github_repository)[1]

  github_subjects = [
    "repo:${local.github_owner}@${var.github_owner_id}/${local.github_repo}@${var.github_repository_id}:environment:deploy",
    "repo:${local.github_owner}@${var.github_owner_id}/${local.github_repo}@${var.github_repository_id}:environment:destroy",
    "repo:${var.github_repository}:environment:deploy",
    "repo:${var.github_repository}:environment:destroy",
  ]
}

# ── State ────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_18: Access logging needs a second bucket, which needs its own state.
  #The bootstrap layer is deliberately the smallest thing that can exist before anything else;
  #CloudTrail data events cover this bucket at the account level, where they belong.
  #checkov:skip=CKV_AWS_144: This is the one layer that outlives every estate. Replicating it
  #would put Terraform state in a second region with a second lifecycle to forget about.
  #checkov:skip=CKV2_AWS_61: State is versioned, not expired. An old state version is how a
  #broken apply is recovered from.
  #checkov:skip=CKV2_AWS_62: Nothing subscribes to state-file writes; Terraform's own locking
  #is the coordination mechanism.
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
  #checkov:skip=CKV2_AWS_64: The default key policy — account root via IAM — is correct here.
  #This key is used by exactly one bucket and one table, both in this layer, and narrowing it
  #further would lock out the SSO identity that has to run the bootstrap apply.
  description             = "${var.project} terraform state"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "state" {
  name          = "alias/${var.project}-tfstate"
  target_key_id = aws_kms_key.state.key_id
}

resource "aws_dynamodb_table" "locks" {
  #checkov:skip=CKV_AWS_119: A lock table holds a hash of a state path and nothing else. A
  #customer-managed key here protects no secret and adds a key to the bootstrap's blast radius.
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

# An account holds at most one provider per issuer, and the GitHub issuer is shared by every
# repository that federates into this account. So this layer adopts an existing provider
# rather than competing for it: with `create_github_oidc_provider = false` the deploy role
# binds to the one already standing, and destroying this layer takes down nothing another
# repository's CI depends on. Creating a provider we do not own is the easier mistake to make
# and the harder one to notice — it only surfaces the day someone runs `destroy`.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = local.github_oidc_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.github_oidc_thumbprints
}

data "aws_iam_policy_document" "assume_from_github" {
  # checkov:skip=CKV_AWS_358: cannot parse GitHub's immutable subject. The check splits the
  # claim on `:` and requires segment 1 to look like `owner/repo`; the format actually issued
  # is `owner@218610429/attestor@1324675810`, and the `@` fails its regex. It inspects only
  # the first value in the list, so the result would turn on the ordering of the array rather
  # than on its contents — a green that means "the element I could read was fine". The real
  # coverage is `scripts/check_oidc_subjects.py`, which reads every value, requires each to
  # name this repository and an environment, and refuses any wildcard anywhere.
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Scoped to this repository *and* to the environments that gate deployment. A wildcard
    # on `sub` would let any workflow in any repository this provider trusts assume the role,
    # which is the single most common way an OIDC setup ends up worse than a static key.
    #
    # `deploy` and `destroy` are hard-coded rather than variable: they have to equal the
    # `environment:` lines in `deploy.yml` and `destroy.yml` exactly, so a knob here is one
    # that silently breaks federation when turned.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.github_subjects
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
  #checkov:skip=CKV_AWS_289: Permissions management is the point — this role creates and
  #destroys the estate's service roles. It is bounded by the `attestor-*` name prefix, by an
  #OIDC trust scoped to one repository and one environment, and by a budget action that
  #detaches its permissions at the ceiling.
  #checkov:skip=CKV_AWS_290: Same. Write access without constraints would be a role that could
  #touch anything; this one is constrained by resource ARN to names this project owns.
  #checkov:skip=CKV_AWS_355: `iam:CreateServiceLinkedRole` has no resource form — AWS requires
  #`*`. It is listed alone rather than folded into the broader statement for that reason.
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
  limit_amount = var.budget_usd
  limit_unit   = "USD"
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
  #checkov:skip=CKV_AWS_289: It is a deny-everything policy. Its whole purpose is breadth.
  #checkov:skip=CKV_AWS_290: See above — the wildcard is the mechanism, not an oversight.
  #checkov:skip=CKV_AWS_355: See above.
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

# ── What CI needs to know, published rather than transcribed ─────────────────
#
# Three of the four values a workflow used to carry as repository configuration are not
# facts — they are consequences of the names this layer already chose. Transcribing them
# into GitHub by hand made them look like independent settings, which is how a renamed
# bucket becomes a deploy that fails on a backend nobody can find.
#
# So they are published here and read at run time. What cannot be published is the account
# id: CI has to know *which* account before it can ask that account anything, and that one
# stays a repository variable. One irreducible value instead of four transcribed ones.
resource "aws_ssm_parameter" "published" {
  #checkov:skip=CKV2_AWS_34: A bucket name, a table name and a role ARN. None is a secret —
  #the boundary is the OIDC trust policy, which is scoped to one repository and one
  #environment, not the confidentiality of these strings.
  #checkov:skip=CKV_AWS_337: Same reason.
  for_each = {
    state_bucket    = aws_s3_bucket.state.id
    lock_table      = aws_dynamodb_table.locks.name
    deploy_role_arn = aws_iam_role.deploy.arn
  }

  name        = "/${var.project}/bootstrap/${each.key}"
  description = "Read by the deploy and destroy workflows once they have assumed the role."
  type        = "String"
  value       = each.value
  tier        = "Standard"
}