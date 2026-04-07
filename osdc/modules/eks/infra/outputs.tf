# Outputs consumed by the justfile and downstream modules.
# Downstream modules can read these via:
#   tofu output -raw <name> (after init with the base state key)

output "cluster_name" {
  value = var.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "aws_region" {
  value = var.aws_region
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "private_subnet_ids" {
  value = module.vpc.private_subnet_ids
}

output "cluster_security_group_id" {
  value = module.eks.cluster_security_group_id
}

output "oidc_provider_arn" {
  value = module.eks.oidc_provider_arn
}

output "oidc_provider" {
  value = module.eks.oidc_provider
}

output "node_instance_role_arn" {
  value = module.eks.node_instance_role_arn
}

# Harbor
output "harbor_s3_bucket" {
  value = module.harbor.s3_bucket_name
}

output "harbor_role_arn" {
  value = module.harbor.role_arn
}

output "harbor_s3_access_key_id" {
  value     = module.harbor.s3_access_key_id
  sensitive = true
}

output "harbor_s3_secret_access_key" {
  value     = module.harbor.s3_secret_access_key
  sensitive = true
}
