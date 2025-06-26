module "arc_runners_vpc" {
  source = "terraform-aws-modules/vpc/aws"
  version = "~> 5.21"

  name = var.arc_prod_environment
  cidr = "10.0.0.0/16"

  azs                     = local.availability_zones
  private_subnets         = ["10.0.0.0/20", "10.0.16.0/20", "10.0.32.0/20"]
  public_subnets          = ["10.0.128.0/20", "10.0.144.0/20", "10.0.160.0/20"]
  map_public_ip_on_launch = false

  tags = {
    Environment         = var.arc_prod_environment
    Project             = var.arc_prod_environment
  }
}
