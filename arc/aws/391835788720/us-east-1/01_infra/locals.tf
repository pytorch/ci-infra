locals {
    availability_zones_loc = ["a", "b", "c", "d", "e", "f"]
    availability_zones        = [
        for loc in local.availability_zones_loc :
        "${local.aws_region}${loc}"
    ]
}
