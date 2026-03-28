module "crcr_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"

  availability_zones                    = local.availability_zones
  aws_region                            = local.aws_region
  cidr_block                            = var.vpc_cidr_block
  cidr_subnet_bits                      = 4
  create_private_hosted_zone            = false
  environment                           = var.environment
  project                               = var.environment
  public_subnet_map_public_ip_on_launch = false
}
