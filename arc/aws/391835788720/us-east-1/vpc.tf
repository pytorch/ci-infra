# ARC
moved {
  from = module.runners_vpc
  to   = module.arc_runners_vpc
}
module "arc_runners_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"
  for_each = {
    for suffix in var.aws_vpc_suffixes:
    suffix => suffix
  }

  availability_zones                    = local.availability_zones
  aws_region                            = local.aws_region
  cidr_block                            = "10.${index(var.aws_vpc_suffixes, each.value)}.0.0/16"
  cidr_subnet_bits                      = 4
  create_private_hosted_zone            = false
  environment                           = "${var.arc_prod_environment}-${each.value}"
  project                               = var.arc_prod_environment
  public_subnet_map_public_ip_on_launch = false
}
moved {
  from = aws_vpc_peering_connection.runners_vpc_peering_connection
  to   = aws_vpc_peering_connection.arc_runners_vpc_peering_connection
}
resource "aws_vpc_peering_connection" "arc_runners_vpc_peering_connection" {
  for_each = {
    for suffix_pair in var.aws_vpc_suffixes_combinations:
    "${suffix_pair[0]}-${suffix_pair[1]}" => suffix_pair
  }
  peer_vpc_id   = module.arc_runners_vpc[each.value[0]].vpc_id
  vpc_id        = module.arc_runners_vpc[each.value[1]].vpc_id
  auto_accept   = true

  accepter {
    allow_remote_vpc_dns_resolution = true
  }

  requester {
    allow_remote_vpc_dns_resolution = true
  }

  tags = {
    Environment = var.arc_prod_environment
  }
}

moved {
  from = module.runners_canary_vpc
  to   = module.arc_runners_canary_vpc
}
module "arc_runners_canary_vpc" {
  source = "../../../tf-modules/terraform-aws-vpc"
  for_each = {
    for suffix in [var.aws_canary_vpc_suffixes[0]]:
    suffix => suffix
  }

  availability_zones                    = local.availability_zones_canary
  aws_region                            = local.aws_region
  cidr_block                            = "10.${index(var.aws_vpc_suffixes, each.value)}.0.0/16"
  cidr_subnet_bits                      = 4
  create_private_hosted_zone            = false
  environment                           = "${var.arc_canary_environment}-${each.value}"
  project                               = var.arc_canary_environment
  public_subnet_map_public_ip_on_launch = false
}
