module "crcr_vpc" {
  source = "../tf-modules/terraform-aws-vpc"

  name = "crcr-vpc-${var.environment}"
  cidr = var.vpc_cidr_block
  azs  = local.availability_zones

  # Equivalent to old module: cidrsubnet(cidr, 4, index)
  public_subnets  = [for i, _ in local.availability_zones : cidrsubnet(var.vpc_cidr_block, 4, i)]
  private_subnets = [for i, _ in local.availability_zones : cidrsubnet(var.vpc_cidr_block, 4, length(local.availability_zones) + i)]

  enable_nat_gateway = true
  single_nat_gateway = true

  map_public_ip_on_launch = false

  tags = local.tags
}
