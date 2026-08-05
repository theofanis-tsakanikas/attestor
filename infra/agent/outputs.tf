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
