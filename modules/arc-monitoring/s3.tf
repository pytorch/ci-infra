resource "aws_s3_bucket" "loki_chunks" {
  bucket = lower("loki-chunks-${var.aws_vpc_suffix}-${var.environment}")

  tags = {
    Project                  = "runners-eks"
    Environment              = var.environment
    Context                  = local.cluster_name
  }
}

resource "aws_s3_bucket" "loki_ruler" {
  bucket = lower("loki-ruler-${var.aws_vpc_suffix}-${var.environment}")

  tags = {
    Project                  = "runners-eks"
    Environment              = var.environment
    Context                  = local.cluster_name
  }
}

resource "aws_s3_bucket" "loki_admin" {
  bucket = lower("loki-admin-${var.aws_vpc_suffix}-${var.environment}")

  tags = {
    Project                  = "runners-eks"
    Environment              = var.environment
    Context                  = local.cluster_name
  }
}
