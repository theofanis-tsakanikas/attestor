output "collection_arn" {
  value = aws_opensearchserverless_collection.main.arn
}
output "evidence_kb_id" {
  value = aws_bedrockagent_knowledge_base.evidence.id
}
output "regulatory_kb_id" {
  value = aws_bedrockagent_knowledge_base.regulatory.id
}
output "guardrail_id" {
  value = aws_bedrock_guardrail.main.guardrail_id
}
output "guardrail_version" {
  value = aws_bedrock_guardrail_version.pinned.version
}
