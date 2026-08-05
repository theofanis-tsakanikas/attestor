output "gold_database" {
  value = aws_glue_catalog_database.gold.name
}
output "ref_database" {
  value = aws_glue_catalog_database.ref.name
}
output "workgroup" {
  value = aws_athena_workgroup.main.name
}
