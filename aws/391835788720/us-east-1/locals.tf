locals {
    availability_zones_loc = ["1a", "1b", "1c", "1d", "1f"]
    availability_zones        = [
        for loc in local.availability_zones_loc :
        "${local.aws_region}-${loc}"
    ]
    availability_zones_canary_loc = ["1b", "1d", "1f"]
    availability_zones_canary = [
        for loc in local.availability_zones_canary_loc :
        "${local.aws_region}-${loc}"
    ]
}
