output "state_bucket" {
  value       = aws_s3_bucket.state.id
  description = "Backend bucket. Every other layer keys its state under a distinct prefix."
}

output "lock_table" {
  value = aws_dynamodb_table.locks.name
}

output "deploy_role_arn" {
  value       = aws_iam_role.deploy.arn
  description = "Set this as the AWS_DEPLOY_ROLE_ARN repository secret."
}

output "kms_key_arn" {
  value = aws_kms_key.state.arn
}
