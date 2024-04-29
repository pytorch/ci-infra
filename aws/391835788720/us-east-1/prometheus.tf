resource "aws_prometheus_workspace" "monitoring_production" {
  alias = "monitoring_production"

  tags = {
    Environment = var.prod_environment
  }
}

resource "aws_prometheus_workspace" "monitoring_canary" {
  alias = "monitoring_canary"

  tags = {
    Environment = var.canary_environment
  }
}
