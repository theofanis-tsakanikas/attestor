output "vpc_id" {
  value = aws_vpc.main.id
}
output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
output "endpoint_security_group_id" {
  value = aws_security_group.endpoints.id
}
output "kms_key_arn" {
  value = aws_kms_key.data.arn
}
output "lake_bucket" {
  value = aws_s3_bucket.lake.id
}
output "evidence_bucket" {
  value = aws_s3_bucket.evidence.id
}
output "reports_bucket" {
  value = aws_s3_bucket.reports.id
}
output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
