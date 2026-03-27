#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Read clusters.yaml and output values for shell consumption.

Usage:
    # Get a single field
    cluster-config.py arc-staging region              -> us-west-1
    cluster-config.py arc-staging cluster_name        -> pytorch-arc-staging
    cluster-config.py arc-staging base.vpc_cidr       -> 10.0.0.0/16

    # Get all base terraform vars as -var flags
    cluster-config.py arc-staging tfvars
        -> -var="cluster_name=pytorch-arc-staging" -var="aws_region=us-west-1" ...

    # Check if a module is enabled
    cluster-config.py arc-staging has-module arc      -> exits 0 (true) or 1 (false)

    # List all cluster IDs
    cluster-config.py --list
"""

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


def tfvars(cluster_id, cluster_cfg, defaults):
    """Produce -var flags for tofu from cluster config."""
    base = {**defaults, **(cluster_cfg.get("base") or {})}
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
    }
    # Optional fields — only emit if explicitly set
    access_config = cluster_cfg.get("access_config") or base.get("access_config") or {}
    if "authentication_mode" in access_config:
        pairs["authentication_mode"] = access_config["authentication_mode"]
    cluster_admin_roles = access_config.get("cluster_admin_role_names", [])
    if cluster_admin_roles:
        pairs["cluster_admin_role_names"] = ",".join(cluster_admin_roles)
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
        else:
            print(val)


if __name__ == "__main__":
    main()
