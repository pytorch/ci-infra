module "runners_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"
  count  = length(var.aws_vpc_suffixes)

  availability_zones                    = var.availability_zones
  aws_region                            = var.aws_region
  create_private_hosted_zone            = false
  environment                           = "${var.prod_environment}-${element(var.aws_vpc_suffixes, count.index)}"
  project                               = var.prod_environment
  public_subnet_map_public_ip_on_launch = true

}
