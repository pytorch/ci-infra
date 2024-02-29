module "runners_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"
  for_each = {
    for suffix in var.aws_vpc_suffixes:
    suffix => suffix
  }

  availability_zones                    = local.availability_zones
  aws_region                            = local.aws_region
  create_private_hosted_zone            = false
  environment                           = "${var.prod_environment}-${each.value}"
  project                               = var.prod_environment
  public_subnet_map_public_ip_on_launch = true
}

module "runners_canary_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"
  for_each = {
    for suffix in [var.aws_canary_vpc_suffixes[0]]:
    suffix => suffix
  }

  availability_zones                    = local.availability_zones_canary
  aws_region                            = local.aws_region
  create_private_hosted_zone            = false
  environment                           = "${var.canary_environment}-${each.value}"
  project                               = var.canary_environment
  public_subnet_map_public_ip_on_launch = true
}
