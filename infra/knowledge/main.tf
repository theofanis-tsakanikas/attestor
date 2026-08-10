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
  evidence_bucket = local.foundation.evidence_bucket
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
  vpc_id             = local.foundation.vpc_id
  subnet_ids         = split(",", local.foundation.private_subnet_ids)
  security_group_ids = [local.foundation.endpoint_security_group_id]
}

resource "aws_opensearchserverless_security_policy" "network" {
  name = "${var.project}-network"
  type = "network"
  policy = jsonencode([
    {
      # Bedrock reaches the collection from its own service network, not through our VPC
      # endpoint, so a VPC-only policy locks out the very service the collection exists for:
      # `CreateKnowledgeBase` fails with `storage configuration provided is invalid...
      # server returned 401`, which reads like a credentials problem and is a routing one.
      #
      # `SourceServices` is the narrow answer. It is not `AllowFromPublic`: the collection is
      # still unreachable from the internet, and everything else still arrives through the
      # endpoint in the private subnets.
      Rules = [
        { ResourceType = "collection", Resource = ["collection/${local.collection}"] },
      ]
      AllowFromPublic = false
      SourceVPCEs     = [aws_opensearchserverless_vpc_endpoint.main.id]
      SourceServices  = ["bedrock.amazonaws.com"]
    },
    {
      # The dashboard rule takes no `SourceServices` — nothing but a human opens it, and a
      # human is on the VPC side or nowhere.
      Rules = [
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
      Principal = [
        aws_iam_role.knowledge_base.arn,
        aws_iam_role.indexer.arn,
        var.deploy_role_arn,
      ]
    }
  ])
}

resource "aws_opensearchserverless_collection" "main" {
  name = local.collection
  type = "VECTORSEARCH"

  # `standby_replicas` doubles billed capacity to buy cross-AZ availability. Off for a build
  # block, because an estate that is rebuilt from this file in half an hour has no uptime to
  # protect; on for a run that must stand up as a real deployment would. Declared per run
  # rather than fixed here, so the topology is a decision somebody made.
  standby_replicas = var.production_topology ? "ENABLED" : "DISABLED"

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
  ]
}

# ── The vector indexes ───────────────────────────────────────────────────────
#
# Bedrock does not create them, and says so only by failing `CreateKnowledgeBase` with
# `storage configuration provided is invalid` — a message that names neither the index nor
# the fact that it is missing. The console papers over this with "quick create"; Terraform
# has no such step, so the step is here.
#
# It is a Lambda in the private subnets rather than a `null_resource` on the runner, because
# the collection is reachable only through its VPC endpoint. Creating the index from CI would
# have meant opening the collection to the internet for one PUT that happens once per estate,
# and a control relaxed for convenience is a control that stays relaxed.

data "archive_file" "indexer" {
  type        = "zip"
  source_dir  = "${path.module}/indexer"
  output_path = "${path.module}/.build/indexer.zip"
}

data "aws_iam_policy_document" "indexer_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "indexer" {
  name               = "${var.project}-indexer"
  assume_role_policy = data.aws_iam_policy_document.indexer_assume.json
}

resource "aws_iam_role_policy" "indexer" {
  name = "create-indexes"
  role = aws_iam_role.indexer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        # `aoss:APIAccessAll` is the data-plane grant. It is not enough on its own — the
        # collection's data access policy names this role too, and both have to agree.
        Action   = ["aoss:APIAccessAll"]
        Resource = aws_opensearchserverless_collection.main.arn
      },
    ]
  })
}

# Logs and the ENI lifecycle come from AWS's own policy rather than a hand-written copy of it.
# Those actions genuinely need `Resource = "*"` — an ENI has no ARN before it is created — so
# writing them out means writing a wildcard and then arguing with a scanner about it. Letting
# AWS own the policy it designed for this is the smaller claim.
resource "aws_iam_role_policy_attachment" "indexer_vpc" {
  role       = aws_iam_role.indexer.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_lambda_function" "indexer" {
  #checkov:skip=CKV_AWS_116: It is invoked synchronously by Terraform during apply. A dead
  #letter queue catches failed *async* invocations, of which there are none.
  #checkov:skip=CKV_AWS_272: Code signing needs a signing profile this estate would create and
  #destroy alongside the function it signs.
  #checkov:skip=CKV_AWS_50: X-Ray on a function that runs twice in an estate's life buys a
  #trace nobody opens.
  function_name    = "${var.project}-indexer"
  role             = aws_iam_role.indexer.arn
  runtime          = "python3.12"
  handler          = "indexer.handler"
  filename         = data.archive_file.indexer.output_path
  source_code_hash = data.archive_file.indexer.output_base64sha256
  # Long, because it waits for a fresh collection's data plane to accept writes and then for
  # the new index to become visible to Bedrock. Both are real waits, not padding.
  timeout                        = 300
  reserved_concurrent_executions = 1

  # No `environment` block. The one value it would carry — the embedding dimension — travels
  # in the invocation payload instead, where it belongs: it describes the index being built,
  # not the function building it.

  vpc_config {
    subnet_ids         = split(",", local.foundation.private_subnet_ids)
    security_group_ids = [local.foundation.endpoint_security_group_id]
  }
}

resource "aws_lambda_invocation" "indexes" {
  function_name = aws_lambda_function.indexer.function_name

  input = jsonencode({
    endpoint = aws_opensearchserverless_collection.main.collection_endpoint
    indexes = [
      for name in ["evidence", "regulatory"] : {
        name           = "${var.project}-${name}"
        vector_field   = "embedding"
        text_field     = "text"
        metadata_field = "metadata"
        dimension      = var.embedding_dimension
      }
    ]
  })

  depends_on = [
    aws_opensearchserverless_access_policy.data,
    aws_iam_role_policy.indexer,
    aws_iam_role_policy_attachment.indexer_vpc,
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
        # The evidence bucket is SSE-KMS, so `s3:GetObject` alone reads nothing. Without this
        # the ingestion job completes — `status: COMPLETE` — with every document counted as
        # failed and none indexed, and the only place that says why is `failureReasons` on the
        # job itself. Downstream it surfaces as a knowledge base that answers every query with
        # silence, which the narrative layer reports as "no deliverable evidence".
        #
        # A job that succeeds while indexing nothing is the worst shape available, and it is
        # why `kb-sync.sh` should fail on `numberOfDocumentsFailed` rather than on status.
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = local.foundation.kms_key_arn
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
      vector_index_name = "${var.project}-evidence"
      field_mapping {
        vector_field   = "embedding"
        text_field     = "text"
        metadata_field = "metadata"
      }
    }
  }

  # The index has to exist before this resource is created, and Bedrock reports its absence
  # as a malformed storage configuration rather than as a missing index.
  depends_on = [aws_lambda_invocation.indexes]
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
      vector_index_name = "${var.project}-regulatory"
      field_mapping {
        vector_field   = "embedding"
        text_field     = "text"
        metadata_field = "metadata"
      }
    }
  }

  # The index has to exist before this resource is created, and Bedrock reports its absence
  # as a malformed storage configuration rather than as a missing index.
  depends_on = [aws_lambda_invocation.indexes]
}

# Tenant corpora are separate data sources into one index, separated by metadata rather than
# by index. One index per tenant would cost an index per onboarding, which is where a
# multi-tenant platform stops scaling — and the isolation suite tests the filter, which is
# the control that would have to hold either way.
resource "aws_bedrockagent_data_source" "tenant" {
  for_each = toset(var.tenants)

  knowledge_base_id = aws_bedrockagent_knowledge_base.evidence.id
  name              = "${var.project}-evidence-${each.value}"
  # RETAIN, not DELETE. `DELETE` asks Bedrock to walk the index and remove this data source's
  # vectors before the data source itself goes — and the collection it is walking is being
  # destroyed in the same run. Destroy `31188800462` died exactly there: `DELETE_UNSUCCESSFUL —
  # Unable to delete data from vector store`, leaving the collection, the knowledge base and the
  # whole foundation layer standing and billing.
  #
  # Nothing is retained in practice. The collection is destroyed wholesale a few resources
  # later, and the index inside it with it. `DELETE` was buying a tidy intermediate state that
  # no one observes, at the price of a teardown that cannot finish.
  data_deletion_policy = "RETAIN"

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

# The regulatory corpus. Shared across tenants, so it carries no tenant metadata and lives
# under a prefix no tenant can be allocated — a tenant id must start with a letter, and this
# one starts with an underscore.
#
# It is here because it was not: the regulatory knowledge base was created with no data
# source at all. Terraform applied cleanly, `kb-sync.sh` iterated an empty data-source list
# and returned success, and `search_regulation` answered every query with nothing. Every
# layer reported healthy and one of the two corpora did not exist.
resource "aws_bedrockagent_data_source" "regulatory" {
  knowledge_base_id = aws_bedrockagent_knowledge_base.regulatory.id
  name              = "${var.project}-regulatory"
  # RETAIN, not DELETE. `DELETE` asks Bedrock to walk the index and remove this data source's
  # vectors before the data source itself goes — and the collection it is walking is being
  # destroyed in the same run. Destroy `31188800462` died exactly there: `DELETE_UNSUCCESSFUL —
  # Unable to delete data from vector store`, leaving the collection, the knowledge base and the
  # whole foundation layer standing and billing.
  #
  # Nothing is retained in practice. The collection is destroyed wholesale a few resources
  # later, and the index inside it with it. `DELETE` was buying a tidy intermediate state that
  # no one observes, at the price of a teardown that cannot finish.
  data_deletion_policy = "RETAIN"

  data_source_configuration {
    type = "S3"
    s3_configuration {
      bucket_arn         = "arn:aws:s3:::${local.evidence_bucket}"
      inclusion_prefixes = ["_shared/regulatory/"]
    }
  }

  # Hierarchical, like the evidence corpus, and for a sharper reason: a clause reference such
  # as "§44(a)" is meaningless without "ESRS E1-6" above it, so the parent level has to travel
  # with the chunk or retrieval matches the wrong article with high confidence.
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
  name = "${var.project}-guardrail"
  # Set because omitting it fails the apply outright: the provider returns `description` as
  # still-unknown afterwards, and Terraform treats an unknown value after apply as a provider
  # bug and aborts — "Provider returned invalid result object after apply". The guardrail is
  # created by then, so the failure lands after the side effect, which is the worst shape a
  # failure can have. A description is worth writing anyway.
  description               = "Blocks prompt-attack inputs and the disclosure of one tenant's evidence in another tenant's report."
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

# ── What this layer offers the agent layer ───────────────────────────────────
#
# Same mechanism as `infra/foundation`, and for the same reason: a neighbour should read a
# named contract, not this layer's whole state file. See the note there.
locals {
  published = {
    collection_arn    = aws_opensearchserverless_collection.main.arn
    evidence_kb_id    = aws_bedrockagent_knowledge_base.evidence.id
    regulatory_kb_id  = aws_bedrockagent_knowledge_base.regulatory.id
    guardrail_id      = aws_bedrock_guardrail.main.guardrail_id
    guardrail_version = aws_bedrock_guardrail_version.pinned.version
  }
}

resource "aws_ssm_parameter" "published" {
  #checkov:skip=CKV2_AWS_34: Identifiers, not secrets — two knowledge-base ids, a guardrail
  #id and its pinned version. See the equivalent note in infra/foundation.
  #checkov:skip=CKV_AWS_337: Same reason.
  for_each = local.published

  name        = "/${var.project}/knowledge/${each.key}"
  description = "Cross-layer reference published by infra/knowledge."
  type        = "String"
  value       = each.value
  tier        = "Standard"
}
