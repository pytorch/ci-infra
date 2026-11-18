#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Read clusters.yaml and output values for shell consumption.

Usage:
    # Get a single field
    cluster-config.py meta-staging-aws-ue1 region              -> us-east-1
    cluster-config.py meta-staging-aws-ue1 cluster_name        -> meta-staging-aws-ue1
    cluster-config.py meta-staging-aws-ue1 base.vpc_cidr       -> 10.20.0.0/16

    # Get all base terraform vars as -var flags
    cluster-config.py meta-staging-aws-ue1 tfvars
        -> -var="cluster_name=meta-staging-aws-ue1" -var="aws_region=us-east-1" ...

    # Check if a module is enabled
    cluster-config.py meta-staging-aws-ue1 has-module arc      -> exits 0 (true) or 1 (false)

    # List all cluster IDs
    cluster-config.py --list
"""

import ipaddress
import os
import sys
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("CLUSTERS_YAML", Path(__file__).resolve().parent.parent / "clusters.yaml"))


def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def resolve(cluster_cfg, defaults, dotpath):
    """Resolve a dot-separated path like 'base.vpc_cidr' against cluster config with defaults."""
    parts = dotpath.split(".")
    val = cluster_cfg
    dval = defaults
    for part in parts:
        val = val.get(part) if isinstance(val, dict) else None
        dval = dval.get(part) if isinstance(dval, dict) else None
    if val is not None:
        return val
    return dval


def availability_zone_vars(azs, vpc_cidr):
    """Turn base.availability_zones into comma-separated tofu vars.

    Each entry is {zone_id, private_cidr, public_cidr}. Order matters: the VPC
    module indexes subnets, NAT gateways and route tables by position, and the
    first three entries are the EKS cluster's AZs. Raises ValueError on a
    malformed list, since tofu would only catch most of these at apply time.
    """
    if not isinstance(azs, list) or len(azs) < 2:
        raise ValueError("base.availability_zones must list at least 2 AZs (EKS needs subnets in 2 AZs)")
    keys = ("zone_id", "private_cidr", "public_cidr")
    for i, az in enumerate(azs):
        if not isinstance(az, dict) or any(not az.get(k) for k in keys):
            raise ValueError(f"base.availability_zones[{i}] must set {', '.join(keys)}: {az!r}")
    zone_ids = [az["zone_id"] for az in azs]
    if len(set(zone_ids)) != len(zone_ids):
        raise ValueError(f"base.availability_zones has duplicate zone IDs: {zone_ids}")
    vpc = ipaddress.ip_network(vpc_cidr)
    subnets = []
    for az in azs:
        for key in ("private_cidr", "public_cidr"):
            net = ipaddress.ip_network(az[key])
            if not net.subnet_of(vpc):
                raise ValueError(f"{az['zone_id']} {key} {net} is outside vpc_cidr {vpc}")
            clash = next((s for s in subnets if s.overlaps(net)), None)
            if clash:
                raise ValueError(f"{az['zone_id']} {key} {net} overlaps {clash}")
            subnets.append(net)
    return {
        "availability_zone_ids": ",".join(zone_ids),
        "private_subnet_cidrs": ",".join(az["private_cidr"] for az in azs),
        "public_subnet_cidrs": ",".join(az["public_cidr"] for az in azs),
    }


def tfvars(cluster_id, cluster_cfg, defaults):
    """Produce -var flags for tofu from cluster config."""
    base = {**defaults, **(cluster_cfg.get("base") or {})}
    # Resolve nested config (e.g. coredns.replicas) using the same lookup
    # rules as resolve(): cluster value wins; otherwise defaults.
    coredns_replicas = resolve(cluster_cfg, defaults, "coredns.replicas")
    if coredns_replicas is None:
        coredns_replicas = 6
    pairs = {
        "cluster_name": cluster_cfg["cluster_name"],
        "aws_region": cluster_cfg["region"],
        "vpc_cidr": base.get("vpc_cidr", "10.0.0.0/16"),
        "single_nat_gateway": str(base.get("single_nat_gateway", False)).lower(),
        "base_node_count": base.get("base_node_count", 3),
        "base_node_instance_type": base.get("base_node_instance_type", "m5.xlarge"),
        "base_node_max_unavailable_percentage": base.get("base_node_max_unavailable_percentage", 33),
        "base_node_ami_version": base.get("base_node_ami_version", defaults.get("base_node_ami_version", "v*")),
        "eks_version": base.get("eks_version", defaults.get("eks_version", "1.35")),
        "coredns_replicas": coredns_replicas,
    }
    # Optional fields — only emit if explicitly set
    access_config = cluster_cfg.get("access_config") or base.get("access_config") or {}
    if "authentication_mode" in access_config:
        pairs["authentication_mode"] = access_config["authentication_mode"]
    cluster_admin_roles = access_config.get("cluster_admin_role_names", [])
    if cluster_admin_roles:
        pairs["cluster_admin_role_names"] = ",".join(cluster_admin_roles)
    # Cost knobs for small clusters. Emitted only when the cluster (or defaults)
    # sets them, so existing clusters keep the terraform defaults (prod shape).
    for key in ("nat_secondary_eip_count", "base_node_subnet_count"):
        if base.get(key) is not None:
            pairs[key] = base[key]
    if "cluster_log_types" in base:
        # An empty list is meaningful: it disables control-plane logging.
        pairs["cluster_log_types"] = ",".join(base.get("cluster_log_types") or [])
    # Explicit AZ list. Without it, tofu uses the first three AZs the region lists.
    if base.get("availability_zones"):
        try:
            pairs.update(availability_zone_vars(base["availability_zones"], pairs["vpc_cidr"]))
        except ValueError as e:
            print(f"Error: cluster '{cluster_id}': {e}", file=sys.stderr)
            sys.exit(1)
    flags = [f'-var="{k}={v}"' for k, v in pairs.items()]
    print(" ".join(flags))


def main():
    cfg = load_config()
    defaults = cfg.get("defaults", {})
    clusters = cfg.get("clusters", {})

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <cluster-id> <field|tfvars|has-module> [value]", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--list":
        for cid in clusters:
            print(cid)
        return

    cluster_id = sys.argv[1]
    if cluster_id not in clusters:
        print(f"Error: unknown cluster '{cluster_id}'. Known: {', '.join(clusters.keys())}", file=sys.stderr)
        sys.exit(1)

    cluster_cfg = clusters[cluster_id]
    cmd = sys.argv[2] if len(sys.argv) > 2 else "cluster_name"

    if cmd == "tfvars":
        tfvars(cluster_id, cluster_cfg, defaults)
    elif cmd == "has-module":
        module = sys.argv[3] if len(sys.argv) > 3 else ""
        modules = cluster_cfg.get("modules", [])
        sys.exit(0 if module in modules else 1)
    elif cmd == "enabled-modules":
        print(" ".join(cluster_cfg.get("modules", []) or []))
    elif cmd == "modules":
        for m in cluster_cfg.get("modules", []):
            print(m)
    elif cmd == "state_bucket":
        print(cluster_cfg.get("state_bucket", f"ciforge-tfstate-{cluster_id}"))
    elif cmd == "region":
        print(cluster_cfg["region"])
    elif cmd == "cluster_name":
        print(cluster_cfg["cluster_name"])
    else:
        default_val = sys.argv[3] if len(sys.argv) > 3 else None
        val = resolve(cluster_cfg, defaults, cmd)
        if val is None:
            if default_val is not None:
                print(default_val)
            else:
                print(f"Error: field '{cmd}' not found", file=sys.stderr)
                sys.exit(1)
        elif isinstance(val, bool):
            print(str(val).lower())
        elif isinstance(val, list):
            print(",".join(str(v) for v in val))
        elif isinstance(val, dict):
            # Ordered "key:value,key:value" — e.g. buildkit instance_types
            # maps an instance type to its pods-per-node.
            print(",".join(f"{k}:{v}" for k, v in val.items()))
        else:
            print(val)


if __name__ == "__main__":
    main()
