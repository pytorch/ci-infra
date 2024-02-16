resource "aws_s3_bucket" "internal_docker_registry" {
  bucket = "internal-docker-registry-${var.environment}-${lower(var.aws_vpc_suffix)}"
  force_destroy = true

  tags = {
    Project                  = "runners-eks"
    Environment              = var.environment
    Context                  = local.cluster_name
  }
}
