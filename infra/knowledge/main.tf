# Knowledge — the vector store, two Knowledge Bases, and the guardrail.
#
# OpenSearch Serverless is the largest single line item in the estate and the choice is
# deliberate. The alternative stores are cheaper at rest and cannot do the two things this
# system needs: hybrid search, so "E1-6 §44(a)" finds the article when a user types its
# number rather than describes it; and metadata filtering evaluated *at the index*, so the
# tenant filter is a query constraint rather than a post-filter over rows that were already
# read. The second is a security property, and paying for it is not optional.
#
# Its idle cost is answered by the estate being ephemeral — stand up, run the bake-off,
# capture, destroy — not by picking a weaker store and calling the difference an optimisation.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
  backend "s3" { key = "knowledge/terraform.tfstate" }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "attestor:layer"      = "knowledge"
      "attestor:managed"    = "terraform"
      "attestor:expires-at" = var.expires_at
    }
  }
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

locals {
  evidence_bucket = data.terraform_remote_state.foundation.outputs.evidence_bucket
  collection      = "${var.project}-vectors"
}

# ── OpenSearch Serverless ────────────────────────────────────────────────────

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${var.project}-encryption"
  type = "encryption"
  policy = jsonencode({
    Rules       = [{ ResourceType = "collection", Resource = ["collection/${local.collection}"] }]
    AWSOwnedKey = true
  })
}

# No public network access. The collection is reachable only through a VPC endpoint, which
# means a leaked credential is not by itself enough to read a tenant's corpus from a laptop.
resource "aws_opensearchserverless_vpc_endpoint" "main" {
  name               = "${var.project}-aoss"
  vpc_id             = data.terraform_remote_state.foundation.outputs.vpc_id
  subnet_ids         = data.terraform_remote_state.foundation.outputs.private_subnet_ids
  security_group_ids = [data.terraform_remote_state.foundation.outputs.endpoint_security_group_id]
}

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${var.project}-network"
  type = "network"
  policy = jsonencode([
    {
      Rules = [
        { ResourceType = "collection", Resource = ["collection/${local.collection}"] },
        { ResourceType = "dashboard", Resource = ["collection/${local.collection}"] },
      ]
      AllowFromPublic = false
      SourceVPCEs     = [aws_opensearchserverless_vpc_endpoint.main.id]
    }
  ])
}

resource "aws_opensearchserverless_access_policy" "data" {
  name = "${var.project}-data"
  type = "data"
  policy = jsonencode([
    {
      Rules = [
        {
          ResourceType = "index"
          Resource     = ["index/${local.collection}/*"]
          Permission   = ["aoss:*"]
        },
        {
          ResourceType = "collection"
          Resource     = ["collection/${local.collection}"]
          Permission   = ["aoss:*"]
        },
      ]
      Principal = [aws_iam_role.knowledge_base.arn, var.deploy_role_arn]
    }
  ])
}

resource "aws_opensearchserverless_collection" "main" {
  name = local.collection
  type = "VECTORSEARCH"

  # `standby_replicas` doubles capacity for availability this estate does not need. It is
  # turned off for the build blocks and turned on for the capture run, which is the one place
  # the production topology has to be the thing that was photographed.
  standby_replicas = var.production_topology ? "ENABLED" : "DISABLED"

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
  ]
}

# ── Knowledge Bases ──────────────────────────────────────────────────────────

data "aws_iam_policy_document" "kb_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "knowledge_base" {
  name               = "${var.project}-knowledge-base"
  assume_role_policy = data.aws_iam_policy_document.kb_assume.json
}

resource "aws_iam_role_policy" "knowledge_base" {
  name = "read-corpus-and-embed"
  role = aws_iam_role.knowledge_base.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model}"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = ["arn:aws:s3:::${local.evidence_bucket}", "arn:aws:s3:::${local.evidence_bucket}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["aoss:APIAccessAll"]
        Resource = aws_opensearchserverless_collection.main.arn
      },
    ]
  })
}

resource "aws_bedrockagent_knowledge_base" "evidence" {
  name     = "${var.project}-evidence"
  role_arn = aws_iam_role.knowledge_base.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model}"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.main.arn
      vector_index_name = "attestor-evidence"
      field_mapping {
        vector_field   = "embedding"
        text_field     = "text"
        metadata_field = "metadata"
      }
    }
  }
}

resource "aws_bedrockagent_knowledge_base" "regulatory" {
  name     = "${var.project}-regulatory"
  role_arn = aws_iam_role.knowledge_base.arn

  knowledge_base_configuration {
    type = "VECTOR"
    vector_knowledge_base_configuration {
      embedding_model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.embedding_model}"
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"
    opensearch_serverless_configuration {
      collection_arn    = aws_opensearchserverless_collection.main.arn
      vector_index_name = "attestor-regulatory"
      field_mapping {
        vector_field   = "embedding"
        text_field     = "text"
        metadata_field = "metadata"
      }
    }
  }
}

# Tenant corpora are separate data sources into one index, separated by metadata rather than
# by index. One index per tenant would cost an index per onboarding, which is where a
# multi-tenant platform stops scaling — and the isolation suite tests the filter, which is
# the control that would have to hold either way.
resource "aws_bedrockagent_data_source" "tenant" {
  for_each = toset(var.tenants)

  knowledge_base_id    = aws_bedrockagent_knowledge_base.evidence.id
  name                 = "${var.project}-evidence-${each.value}"
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = "arn:aws:s3:::${local.evidence_bucket}"
      inclusion_prefixes = ["${each.value}/"]
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "HIERARCHICAL"
      hierarchical_chunking_configuration {
        overlap_tokens = 60
        level_configuration {
          max_tokens = 1500
        }
        level_configuration {
          max_tokens = 300
        }
      }
    }
  }
}

# ── Guardrail ────────────────────────────────────────────────────────────────

# The guardrail is defence in depth, not the boundary. Nothing here decides whether a figure
# may be published — that is the resolver and the provenance gate. What it does is stop
# personal data reaching a model prompt, and refuse the categories of request that have no
# legitimate place in a reporting conversation.
resource "aws_bedrock_guardrail" "main" {
  name                      = "${var.project}-guardrail"
  blocked_input_messaging   = "This request cannot be processed by the reporting assistant."
  blocked_outputs_messaging = "This response was withheld by policy."

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
  }

  sensitive_information_policy_config {
    pii_entities_config {
      type   = "EMAIL"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "NAME"
      action = "ANONYMIZE"
    }
    pii_entities_config {
      type   = "AWS_ACCESS_KEY"
      action = "BLOCK"
    }
    pii_entities_config {
      type   = "AWS_SECRET_KEY"
      action = "BLOCK"
    }
  }

  topic_policy_config {
    topics_config {
      name       = "financial-advice"
      type       = "DENY"
      definition = "Recommendations about investing in, or divesting from, the undertaking."
      examples   = ["Should we buy shares in this company based on its emissions?"]
    }
  }

  # Contextual grounding at the threshold the contracts declare. The two are cross-checked
  # in CI, so raising one without the other fails the build rather than silently loosening
  # what a narrative is allowed to assert.
  contextual_grounding_policy_config {
    filters_config {
      type      = "GROUNDING"
      threshold = var.grounding_threshold
    }
    filters_config {
      type      = "RELEVANCE"
      threshold = var.relevance_threshold
    }
  }
}

resource "aws_bedrock_guardrail_version" "pinned" {
  guardrail_arn = aws_bedrock_guardrail.main.guardrail_arn
  description   = "Pinned. An agent must never point at DRAFT: a draft changes without review."
}
