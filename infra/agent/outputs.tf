output "tools_function_arn" {
  value = aws_lambda_function.tools.arn
}
output "tools_role_arn" {
  value = aws_iam_role.tools.arn
}
output "user_pool_ids" {
  value = { for k, v in aws_cognito_user_pool.tenant : k => v.id }
}
output "log_group" {
  value = aws_cloudwatch_log_group.agent.name
}

output "gateway_url" {
  value       = { for k, g in awscc_bedrockagentcore_gateway.tenant : k => g.gateway_url }
  description = "The MCP endpoint an agent connects to."
}

output "policy_engine_arn" {
  value = awscc_bedrockagentcore_policy_engine.main.policy_engine_arn
}

output "deployed_policies" {
  value       = [for p in awscc_bedrockagentcore_policy.cedar : p.name]
  description = "Deployed verbatim from policy/cedar/. The offline evaluator reads the same files."
}

output "memory_ids" {
  value = { for k, v in awscc_bedrockagentcore_memory.tenant : k => v.memory_id }
}

output "ecr_repository_name" {
  description = "For `aws ecr` calls, which take a name where docker takes a URL."
  value       = aws_ecr_repository.agent.name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "runtime_endpoint_arn" {
  value = try(awscc_bedrockagentcore_runtime_endpoint.live[0].agent_runtime_endpoint_arn, null)
}

output "reasoning_model" {
  value       = var.reasoning_model
  description = "The model the narrative provider drafts with. Read by the deploy workflow so the live run cannot silently fall back to a recorded draft."
}

output "verification_secret_names" {
  description = <<-EOT
    Where the deploy finds the credential it authenticates as, per tenant. The password itself
    is never an output: an output is written to state and printed by `terraform output`, and a
    credential that appears in a log is a credential.
  EOT
  value       = { for tenant, s in aws_secretsmanager_secret.verification : tenant => s.name }
}

output "tenant_issuers" {
  description = <<-EOT
    The issuer each tenant's tokens carry, which is also what the deployed handler checks them
    against. Published so a verification step can assert the deployment and the account agree
    — the mismatch this replaces was invisible for as long as nothing called the gateway.
  EOT
  value       = { for tenant, pool in aws_cognito_user_pool.tenant : tenant => "https://cognito-idp.${var.region}.amazonaws.com/${pool.id}" }
}
