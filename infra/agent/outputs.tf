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
  value       = awscc_bedrockagentcore_gateway.main.gateway_url
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

output "ecr_repository_url" {
  value = aws_ecr_repository.agent.repository_url
}

output "runtime_endpoint_arn" {
  value = try(awscc_bedrockagentcore_runtime_endpoint.live[0].agent_runtime_endpoint_arn, null)
}
