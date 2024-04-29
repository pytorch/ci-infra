locals {
    availability_zones_loc = ["a", "b", "c", "d", "f"]
    availability_zones        = [
        for loc in local.availability_zones_loc :
        "${local.aws_region}${loc}"
    ]
    availability_zones_canary_loc = ["b", "d", "f"]
    availability_zones_canary = [
        for loc in local.availability_zones_canary_loc :
        "${local.aws_region}${loc}"
    ]
}
