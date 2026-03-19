#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Collect actual Kubernetes node memory capacity by instance type.

Queries the cluster for all nodes, extracts their reported memory capacity
(status.capacity.memory), and groups by instance type. Cross-references
against INSTANCE_SPECS in analyze_node_utilization.py to identify gaps and
emit a ready-to-paste dict for updating memory_mi values.

Usage:
    uv run scripts/python/collect_instance_memory.py [--kubeconfig PATH]
"""

import argparse
import json
import os
import subprocess
import sys

from instance_specs import INSTANCE_SPECS


def ki_to_mib(ki_str: str) -> int:
    """Convert Kubernetes Ki memory string to MiB, rounded to int."""
    value = ki_str.strip()
    if value.endswith("Ki"):
        return int(int(value[:-2]) / 1024)
    # Fallback: treat as bytes
    return int(int(value) / (1024 * 1024))


def collect_node_memory(kubeconfig: str | None) -> dict[str, list[int]]:
    """Run kubectl get nodes -o json and return instance_type -> list of memory_mi."""
    cmd = ["kubectl", "get", "nodes", "-o", "json"]
    env_extra = {}
    if kubeconfig:
        env_extra["KUBECONFIG"] = kubeconfig

    env = {**os.environ, **env_extra}

    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        print(f"ERROR: kubectl failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(result.stdout)
    by_type: dict[str, list[int]] = {}

    for item in data.get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        instance_type = labels.get("node.kubernetes.io/instance-type")
        if not instance_type:
            continue

        capacity = item.get("status", {}).get("capacity", {})
        memory_raw = capacity.get("memory")
        if not memory_raw:
            continue

        memory_mi = ki_to_mib(memory_raw)
        by_type.setdefault(instance_type, []).append(memory_mi)

    return by_type


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect actual node memory capacity by instance type from the cluster",
    )
    parser.add_argument(
        "--kubeconfig",
        metavar="PATH",
        help="Path to kubeconfig file (default: use KUBECONFIG env / ~/.kube/config)",
    )
    args = parser.parse_args(argv)

    print("Querying cluster for node memory capacity...\n")
    by_type = collect_node_memory(args.kubeconfig)

    # Cross-reference against INSTANCE_SPECS
    print("Instance type memory (actual vs INSTANCE_SPECS):")
    print(f"  {'Instance Type':<26} {'Actual MiB':>12} {'Spec MiB':>12} {'Match':>8}")
    print(f"  {'─' * 62}")

    for instance_type in sorted(INSTANCE_SPECS.keys()):
        spec_mi = INSTANCE_SPECS[instance_type]["memory_mi"]
        memory_gib = INSTANCE_SPECS[instance_type]["memory_gib"]
        estimate = int(memory_gib * 1024 * 0.925)

        if instance_type in by_type:
            samples = by_type[instance_type]
            # Use the minimum (conservative: Kubernetes reports capacity, not allocatable)
            actual_mi = min(samples)
            match = "YES" if actual_mi == spec_mi else "NO"
            print(f"  {instance_type:<26} {actual_mi:>12} {spec_mi:>12} {match:>8}")
        else:
            print(f"  {instance_type:<26} {'NOT PRESENT':>12} {spec_mi:>12} {'—':>8}")
            print(f"  {'':26}   (using estimate: {estimate})")

    # Report instance types found in the cluster but not in INSTANCE_SPECS
    unknown = sorted(set(by_type) - set(INSTANCE_SPECS))
    if unknown:
        print("\nInstance types found in cluster but NOT in INSTANCE_SPECS:")
        for it in unknown:
            samples = by_type[it]
            actual_mi = min(samples)
            print(f"  {it}: {actual_mi} MiB  (add to INSTANCE_SPECS)")

    # Emit ready-to-paste dict
    print("\n" + "─" * 70)
    print("# Ready-to-paste memory_mi values for INSTANCE_SPECS:\n")
    print("INSTANCE_SPECS_MEMORY_MI = {")
    for instance_type in sorted(INSTANCE_SPECS.keys()):
        memory_gib = INSTANCE_SPECS[instance_type]["memory_gib"]
        estimate = int(memory_gib * 1024 * 0.925)
        if instance_type in by_type:
            actual_mi = min(by_type[instance_type])
            comment = "# actual from cluster"
        else:
            actual_mi = estimate
            comment = "# NOT PRESENT — using estimate: int(memory_gib * 1024 * 0.925)"
        print(f'    "{instance_type}": {actual_mi},  {comment}')
    print("}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
