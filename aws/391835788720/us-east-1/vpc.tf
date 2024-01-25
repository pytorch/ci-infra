module "runners_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"
  for_each = {
    for i in range(0, length(var.aws_vpc_suffixes)):
    element(var.aws_vpc_suffixes, i) => element(var.aws_vpc_suffixes, i)
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
    for i in range(0, length(var.aws_vpc_suffixes)):
    element(var.aws_vpc_suffixes, i) => element(var.aws_vpc_suffixes, i)
  }

  availability_zones                    = local.availability_zones_canary
  aws_region                            = local.aws_region
  create_private_hosted_zone            = false
  environment                           = "${var.prod_environment}-${each.value}"
  project                               = var.prod_environment
  public_subnet_map_public_ip_on_launch = true
}
