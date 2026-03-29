"""Discover DaemonSet resource overhead from project Kubernetes manifests.

Scans base/kubernetes/ and modules/*/kubernetes/ for DaemonSet documents,
parses their resource requests, and detects GPU-only targeting.  Supplements
with constants for Helm-deployed and EKS-managed DaemonSets that don't live
in raw YAML manifests.

Usage (standalone):
    uv run scripts/python/daemonset_overhead.py [--upstream-dir DIR]

Usage (library):
    from daemonset_overhead import discover_daemonsets, DaemonSetOverhead
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class DaemonSetOverhead:
    """Resource overhead of a single DaemonSet on runner nodes."""

    name: str
    cpu_millicores: int
    memory_mib: int
    gpu_only: bool
    source: str  # e.g. "base/kubernetes/nvidia-device-plugin.yaml" or "constant:helm"


# ---------------------------------------------------------------------------
# Constants for DaemonSets not discoverable from raw YAML manifests
# ---------------------------------------------------------------------------

# Helm-deployed DaemonSets — values from their respective Helm charts
HELM_DAEMONSETS: list[DaemonSetOverhead] = [
    # kube-prometheus-stack node-exporter (chart defaults)
    # Values from modules/monitoring/helm/values.yaml
    DaemonSetOverhead("node-exporter", 15, 32, False, "constant:helm:kube-prometheus-stack"),
    # Alloy logging DaemonSet
    # Values from modules/logging/helm/alloy-logging-values.yaml
    DaemonSetOverhead("alloy-logging", 100, 256, False, "constant:helm:alloy-logging"),
]

# EKS-managed addon DaemonSets — not in our manifests at all
EKS_ADDON_DAEMONSETS: list[DaemonSetOverhead] = [
    DaemonSetOverhead("kube-proxy", 50, 80, False, "constant:eks-addon"),
    DaemonSetOverhead("vpc-cni", 50, 128, False, "constant:eks-addon"),
    DaemonSetOverhead("ebs-csi-node", 10, 50, False, "constant:eks-addon"),
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_cpu_millicores(value: str | int | float) -> int:
    """Parse a Kubernetes CPU resource string to millicores.

    Examples: "100m" -> 100, "2" -> 2000, 0.5 -> 500
    """
    s = str(value)
    if s.endswith("m"):
        return int(s[:-1])
    return int(float(s) * 1000)


def parse_memory_mib(value: str | int | float) -> int:
    """Parse a Kubernetes memory resource string to MiB.

    Examples: "256Mi" -> 256, "4Gi" -> 4096, "1024Ki" -> 1
    """
    s = str(value)
    if s.endswith("Gi"):
        return int(float(s[:-2]) * 1024)
    if s.endswith("Mi"):
        return int(float(s[:-2]))
    if s.endswith("Ki"):
        return int(float(s[:-2]) / 1024)
    # Plain number = bytes
    return int(int(s) / (1024 * 1024))


# ---------------------------------------------------------------------------
# DaemonSet inspection helpers
# ---------------------------------------------------------------------------


def _is_gpu_only(pod_spec: dict) -> bool:
    """Return True if the pod spec targets GPU nodes only.

    Checks for:
    - nodeSelector with a key containing "nvidia.com/gpu"
    - nodeAffinity matchExpressions with a key containing "nvidia.com/gpu"
    """
    # Check nodeSelector
    node_selector = pod_spec.get("nodeSelector", {})
    if node_selector:
        for key in node_selector:
            if "nvidia.com/gpu" in key:
                return True

    # Check nodeAffinity
    affinity = pod_spec.get("affinity", {})
    node_affinity = affinity.get("nodeAffinity", {})
    required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {})
    for term in required.get("nodeSelectorTerms", []):
        for expr in term.get("matchExpressions", []):
            if "nvidia.com/gpu" in expr.get("key", ""):
                return True

    return False


def _extract_container_resources(containers: list[dict]) -> tuple[int, int]:
    """Sum CPU and memory requests across all containers.

    Containers without a resources block contribute 0.
    Only looks at requests (not limits) — requests determine scheduling overhead.
    """
    total_cpu = 0
    total_mem = 0
    for container in containers:
        resources = container.get("resources", {})
        requests = resources.get("requests", {})
        if "cpu" in requests:
            total_cpu += parse_cpu_millicores(requests["cpu"])
        if "memory" in requests:
            total_mem += parse_memory_mib(requests["memory"])
    return total_cpu, total_mem


def _discover_from_yaml(search_dirs: list[Path]) -> list[DaemonSetOverhead]:
    """Scan directories for YAML files containing DaemonSet documents."""
    results: list[DaemonSetOverhead] = []
    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for yaml_file in sorted(search_dir.rglob("*.yaml")):
            try:
                with open(yaml_file) as fh:
                    docs = list(yaml.safe_load_all(fh))
            except (yaml.YAMLError, OSError):
                continue

            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") != "DaemonSet":
                    continue

                name = doc.get("metadata", {}).get("name", yaml_file.stem)
                pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})

                # Sum requests from regular containers only (not initContainers)
                containers = pod_spec.get("containers", [])
                cpu, mem = _extract_container_resources(containers)

                gpu_only = _is_gpu_only(pod_spec)

                # Build a short relative source path
                source = str(yaml_file)
                results.append(DaemonSetOverhead(name, cpu, mem, gpu_only, source))

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_daemonsets(
    upstream_dir: Path,
    consumer_root: Path | None = None,
    include_eks_addons: bool = True,
    include_helm: bool = True,
) -> list[DaemonSetOverhead]:
    """Discover all DaemonSet overhead from project manifests + constants.

    Args:
        upstream_dir: Path to the upstream osdc/ directory.
        consumer_root: Path to the consumer osdc/ directory (if any).
        include_eks_addons: Include EKS-managed addon DaemonSets.
        include_helm: Include Helm-deployed DaemonSets.

    Returns:
        Deduplicated list of DaemonSetOverhead, consumer overriding upstream by name.
    """
    search_dirs = [
        upstream_dir / "base" / "kubernetes",
        upstream_dir / "modules",
    ]

    if consumer_root and consumer_root != upstream_dir:
        consumer_modules = consumer_root / "modules"
        if consumer_modules.exists():
            search_dirs.append(consumer_modules)

    # Discover from YAML manifests
    discovered = _discover_from_yaml(search_dirs)

    # Add constants
    if include_helm:
        discovered.extend(HELM_DAEMONSETS)
    if include_eks_addons:
        discovered.extend(EKS_ADDON_DAEMONSETS)

    # Deduplicate: last entry with a given name wins (consumer overrides upstream)
    seen: dict[str, DaemonSetOverhead] = {}
    for ds in discovered:
        seen[ds.name] = ds

    return list(seen.values())


# ---------------------------------------------------------------------------
# CLI entry point (for debugging / --show-daemonsets equivalent)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover DaemonSet overhead")
    parser.add_argument(
        "--upstream-dir",
        type=Path,
        default=None,
        help="Path to upstream osdc/ directory",
    )
    parser.add_argument(
        "--no-eks-addons",
        action="store_true",
        help="Exclude EKS-managed addon DaemonSets",
    )
    parser.add_argument(
        "--no-helm",
        action="store_true",
        help="Exclude Helm-deployed DaemonSets",
    )
    args = parser.parse_args(argv)

    upstream = args.upstream_dir
    if upstream is None:
        upstream = Path(__file__).resolve().parent.parent.parent

    daemonsets = discover_daemonsets(
        upstream,
        include_eks_addons=not args.no_eks_addons,
        include_helm=not args.no_helm,
    )

    print(f"Discovered {len(daemonsets)} DaemonSets:\n")
    total_cpu = 0
    total_mem = 0
    gpu_cpu = 0
    gpu_mem = 0
    for ds in daemonsets:
        gpu_tag = " [GPU-only]" if ds.gpu_only else ""
        print(f"  {ds.name}: {ds.cpu_millicores}m CPU, {ds.memory_mib}Mi RAM{gpu_tag}")
        print(f"    source: {ds.source}")
        total_cpu += ds.cpu_millicores
        total_mem += ds.memory_mib
        if ds.gpu_only:
            gpu_cpu += ds.cpu_millicores
            gpu_mem += ds.memory_mib

    print(f"\nTotal (all nodes): {total_cpu - gpu_cpu}m CPU, {total_mem - gpu_mem}Mi RAM")
    print(f"Total (GPU nodes): {total_cpu}m CPU, {total_mem}Mi RAM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
