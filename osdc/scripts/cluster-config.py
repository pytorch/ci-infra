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

import json
import os
import re
import sys
from pathlib import Path

import yaml

# Validation patterns for pod_cidr_buckets.
# Bucket names are bucket-1 through bucket-4 (4-bucket architecture).
_BUCKET_NAME_RE = re.compile(r"^bucket-[1-4]$")
# AZ names match AWS canonical format: us-east-2a, eu-west-1c, etc.
_AZ_NAME_RE = re.compile(r"^[a-z]{2}-[a-z]+-\d[a-z]$")
# CIDRs MUST be /16 inside CGNAT 100.64.0.0/10 (octet 64-127). Whole /16
# blocks only — second octet may be 64-127, third/fourth octets must be 0.
_POD_CIDR_RE = re.compile(r"^100\.((6[4-9])|(7[0-9])|(8[0-9])|(9[0-9])|(1[01][0-9])|(12[0-7]))\.0\.0/16$")

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


def _validate_pod_cidr_buckets(cluster_name, buckets):
    """Validate pod_cidr_buckets shape, names, and CIDR uniqueness.

    Raises SystemExit with a clear message on the first violation found.
    Caller is responsible for the missing-key check; this enforces shape
    and content of a present, dict-typed value.
    """
    if not isinstance(buckets, dict) or not buckets:
        raise SystemExit(f"cluster {cluster_name}: base.pod_cidr_buckets must be a non-empty mapping")
    seen_cidrs = {}
    for bucket_name, az_map in buckets.items():
        if not _BUCKET_NAME_RE.match(str(bucket_name)):
            raise SystemExit(
                f"cluster {cluster_name}: invalid bucket name {bucket_name!r} — must match 'bucket-N' where N is 1-4"
            )
        if not isinstance(az_map, dict) or not az_map:
            raise SystemExit(f"cluster {cluster_name}: bucket {bucket_name!r} must map at least one AZ to a CIDR")
        for az_name, cidr in az_map.items():
            if not _AZ_NAME_RE.match(str(az_name)):
                raise SystemExit(
                    f"cluster {cluster_name}: invalid AZ name {az_name!r} in bucket "
                    f"{bucket_name!r} — must match canonical AWS AZ format like 'us-east-2a'"
                )
            if not isinstance(cidr, str) or not _POD_CIDR_RE.match(cidr):
                raise SystemExit(
                    f"cluster {cluster_name}: invalid CIDR {cidr!r} for "
                    f"({bucket_name}, {az_name}) — must be a /16 inside CGNAT 100.64.0.0/10"
                )
            if cidr in seen_cidrs:
                prev_bucket, prev_az = seen_cidrs[cidr]
                raise SystemExit(
                    f"cluster {cluster_name}: duplicate CIDR {cidr} in "
                    f"({bucket_name}, {az_name}); already used by ({prev_bucket}, {prev_az})"
                )
            seen_cidrs[cidr] = (bucket_name, az_name)


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
    flags = [f'-var="{k}={v}"' for k, v in pairs.items()]

    # pod_cidr_buckets is a complex map(map(string)). Required per cluster — no
    # defaults fallback (CIDRs must be unique per VPC). Emit as JSON-encoded
    # tfvar wrapped in single quotes so the inner double quotes survive the
    # shell-eval path in justfile (`eval tofu plan $TFVARS`). Tofu accepts JSON
    # for complex -var values when properly quoted.
    pod_cidr_buckets = (cluster_cfg.get("base") or {}).get("pod_cidr_buckets")
    if pod_cidr_buckets is None:
        raise SystemExit(f"cluster {cluster_id}: missing required base.pod_cidr_buckets")
    _validate_pod_cidr_buckets(cluster_id, pod_cidr_buckets)
    # Use compact separators (no spaces) so `tr ' ' '\n'` in `just info` doesn't
    # split the JSON value across multiple lines.
    flags.append(f"-var='pod_cidr_buckets={json.dumps(pod_cidr_buckets, separators=(',', ':'))}'")

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
