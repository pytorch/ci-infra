#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Generate pypi-cache Kubernetes manifests from clusters.yaml config.

Reads cluster configuration and Kubernetes templates, substitutes placeholders,
and outputs manifests for the Deployment+EFS architecture (one Deployment per
CUDA version plus CPU).

When ``instance_type`` is configured, also generates Karpenter NodePool +
EC2NodeClass manifests for dedicated pypi-cache nodes and computes pod resources
dynamically (same formula as BuildKit).

Outputs:
  storageclass.yaml   — EFS-backed StorageClass
  pvc.yaml            — Shared PersistentVolumeClaim
  deployments.yaml    — Multi-doc YAML (one Deployment per CUDA slug)
  services.yaml       — Multi-doc YAML (one Service per CUDA slug)
  nodepools.yaml      — Karpenter NodePool + EC2NodeClass (when instance_type set)
"""

import argparse
import copy
import math
import sys
from pathlib import Path

import yaml

# analyze_node_utilization and instance_specs live in scripts/python/ at the
# repo root.  Add it to sys.path so the import works both when run directly
# (deploy.sh) and when run via pytest (conftest.py also adds it).
_scripts_python = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _scripts_python not in sys.path:
    sys.path.insert(0, _scripts_python)

from analyze_node_utilization import kubelet_reserved  # noqa: E402
from instance_specs import ENI_MAX_PODS, INSTANCE_SPECS  # noqa: E402

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"

DEFAULTS = {
    "namespace": "pypi-cache",
    "server_port": 8080,
    "internal_port": 8081,
    "image": "pypiserver/pypiserver:v2.4.1",
    "nginx_image": "docker.io/nginxinc/nginx-unprivileged:1.27-alpine",
    "storage_request": "1Ti",
    "replicas": 2,
    "workers": 4,
    "instance_type": "r5d.12xlarge",
    "nginx": {
        "cpu": 8,
        "memory_gi": 2,
        "cache_size": "30Gi",
    },
    "server": {
        "cpu": "500m",
        "memory": "768Mi",
    },
}

# ---------------------------------------------------------------------------
# Overhead constants (milliCPU and MiB)
# ---------------------------------------------------------------------------
# DaemonSet overhead measured from running clusters.  On pypi-cache nodes the
# active DaemonSets are: node-exporter, alloy-logging, efs-csi-node,
# ebs-csi-node, kube-proxy, vpc-cni.  The 10% margin absorbs drift.
DAEMONSET_OVERHEAD_CPU_M = 300
DAEMONSET_OVERHEAD_MEM_MI = 440

# Margin factor — 10% headroom for future growth in DaemonSets/kubelet reserved
MARGIN = 0.90


def compute_pod_resources(instance_type: str, pods_per_node: int) -> dict:
    """Compute per-pod CPU and memory for Guaranteed QoS.

    Formula:
      allocatable = total - kubelet_reserved
      usable = allocatable - daemonset_overhead
      per_pod = floor(usable * margin / pods_per_node)

    Returns dict with ``cpu`` (whole vCPUs) and ``memory_gi`` (whole GiB).
    """
    spec = INSTANCE_SPECS[instance_type]
    vcpu = spec["vcpu"]
    memory_gib = spec["memory_gib"]
    memory_mi = spec["memory_mi"]

    max_pods = ENI_MAX_PODS.get(instance_type, vcpu)
    reserved_cpu_m, reserved_mem_mi = kubelet_reserved(vcpu, memory_gib, max_pods)

    allocatable_cpu_m = vcpu * 1000 - reserved_cpu_m
    allocatable_mem_mi = memory_mi - reserved_mem_mi

    usable_cpu_m = allocatable_cpu_m - DAEMONSET_OVERHEAD_CPU_M
    usable_mem_mi = allocatable_mem_mi - DAEMONSET_OVERHEAD_MEM_MI

    pod_cpu = math.floor(usable_cpu_m * MARGIN / pods_per_node)
    pod_mem_mi = math.floor(usable_mem_mi * MARGIN / pods_per_node)

    # Truncate to whole vCPU and GiB for clean Guaranteed QoS values.
    pod_mem_gi = pod_mem_mi // 1024

    return {
        "cpu": pod_cpu // 1000,
        "memory_gi": pod_mem_gi,
        "allocatable_cpu_m": allocatable_cpu_m,
        "allocatable_mem_mi": allocatable_mem_mi,
    }


def compute_nginx_cache_size(instance_type: str, pods_per_node: int) -> int | None:
    """Compute per-pod nginx cache size in GiB from NVMe storage.

    Returns the usable cache size per pod (95% of NVMe divided by pods),
    or None if the instance has no NVMe storage.
    """
    spec = INSTANCE_SPECS[instance_type]
    nvme_gib = spec.get("nvme_gib", 0)
    if not nvme_gib:
        return None
    return math.floor(nvme_gib * 0.95 / pods_per_node)


def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")


def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}", file=sys.stderr)


def cuda_slug(version: str) -> str:
    """Convert CUDA version to slug using major.minor only.

    '12.1' -> 'cu121', '12.8.1' -> 'cu128', '11.8' -> 'cu118'.
    Patch version is ignored to align with PyTorch convention
    (download.pytorch.org uses /whl/cu128/, not /whl/cu1281/).
    """
    parts = version.split(".")
    return f"cu{parts[0]}{parts[1]}"


def get_slugs(config: dict) -> list[str]:
    """Return list of all CUDA slugs including 'cpu'.

    Always starts with 'cpu', then each cuda_version converted to slug.
    """
    slugs = ["cpu"]
    for ver in config.get("cuda_versions", []):
        slugs.append(cuda_slug(ver))
    return slugs


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = copy.deepcopy(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def load_config(clusters_yaml: Path, cluster_id: str) -> dict:
    """Load and resolve pypi_cache config for a cluster.

    Merge chain: cluster pypi_cache > yaml defaults pypi_cache > hardcoded DEFAULTS.
    """
    with open(clusters_yaml) as f:
        raw = yaml.safe_load(f)

    clusters = raw.get("clusters", {})
    if cluster_id not in clusters:
        known = ", ".join(clusters.keys())
        log_error(f"Unknown cluster '{cluster_id}'. Known: {known}")
        sys.exit(1)

    yaml_defaults = raw.get("defaults", {}).get("pypi_cache", {})
    cluster_overrides = clusters[cluster_id].get("pypi_cache", {})

    # Merge chain: DEFAULTS <- yaml defaults <- cluster overrides
    config = _deep_merge(DEFAULTS, yaml_defaults)
    config = _deep_merge(config, cluster_overrides)
    return config


def generate_storageclass(config: dict, template_path: Path, efs_filesystem_id: str) -> str:
    """Generate StorageClass manifest from template."""
    content = template_path.read_text()
    content = content.replace("__EFS_FILESYSTEM_ID__", efs_filesystem_id)
    content = content.replace("__NAMESPACE__", config["namespace"])
    return content


def generate_pvc(config: dict, template_path: Path) -> str:
    """Generate PersistentVolumeClaim manifest from template."""
    content = template_path.read_text()
    content = content.replace("__NAMESPACE__", config["namespace"])
    content = content.replace("__STORAGE_REQUEST__", str(config["storage_request"]))
    return content


def generate_nodepool(config: dict, template_path: Path) -> str:
    """Generate Karpenter NodePool manifest from template.

    Computes resource limits from replicas and instance specs to allow
    headroom for rolling updates (2x the minimum nodes needed).
    """
    instance_type = config["instance_type"]
    spec = INSTANCE_SPECS[instance_type]

    # Nodes needed = ceil(replicas * slugs / pods_per_node) — but since
    # pods_per_node == len(slugs), this simplifies to replicas.
    # Add 100% headroom for rolling updates.
    nodes_needed = config["replicas"]
    max_nodes = nodes_needed * 2
    cpu_limit = max_nodes * spec["vcpu"]
    memory_limit_gi = max_nodes * spec["memory_gib"]

    content = template_path.read_text()
    content = content.replace("__INSTANCE_TYPE__", instance_type)
    content = content.replace("__CPU_LIMIT__", str(cpu_limit))
    content = content.replace("__MEMORY_LIMIT__", f"{memory_limit_gi}Gi")
    return content


def generate_ec2nodeclass(config: dict, template_path: Path) -> str:
    """Generate EC2NodeClass manifest from template.

    CLUSTER_NAME_PLACEHOLDER is left in — sed-substituted at deploy time.
    Adds instanceStorePolicy: RAID0 for NVMe-equipped instances.
    """
    instance_type = config["instance_type"]
    spec = INSTANCE_SPECS[instance_type]
    content = template_path.read_text()
    content = content.replace("__INSTANCE_TYPE__", instance_type)
    if spec.get("nvme_gib", 0) > 0:
        content = content.replace("__INSTANCE_STORE_POLICY__", "  instanceStorePolicy: RAID0")
    else:
        content = content.replace("__INSTANCE_STORE_POLICY__\n", "")
    return content


def generate_nodepools(config: dict, template_dir: Path) -> str:
    """Generate combined NodePool + EC2NodeClass multi-doc YAML."""
    nodepool = generate_nodepool(config, template_dir / "nodepool.yaml.tpl")
    ec2nodeclass = generate_ec2nodeclass(config, template_dir / "ec2nodeclass.yaml.tpl")
    return nodepool + "\n---\n" + ec2nodeclass


def generate_deployments(config: dict, template_path: Path) -> str:
    """Generate Deployment manifests for all CUDA slugs.

    Each pod has two containers: nginx (caching reverse proxy) and
    pypiserver (gunicorn backend).  When ``instance_type`` is set,
    computes pod resources dynamically — nginx gets a fixed allocation
    and pypiserver gets the remainder.  When absent, uses manual config
    for both containers and CriticalAddonsOnly toleration for shared
    base-infrastructure nodes.

    Returns a multi-document YAML string with --- separators.
    """
    slugs = get_slugs(config)
    instance_type = config.get("instance_type") or ""
    nginx_cfg = config.get("nginx", DEFAULTS["nginx"])

    if instance_type:
        # Dedicated nodes: compute resources from instance specs
        pods_per_node = len(slugs)
        res = compute_pod_resources(instance_type, pods_per_node)

        # nginx gets fixed allocation; pypiserver gets the remainder
        nginx_cpu = nginx_cfg["cpu"]
        nginx_mem_gi = nginx_cfg["memory_gi"]
        server_cpu = res["cpu"] - nginx_cpu
        server_mem_gi = res["memory_gi"] - nginx_mem_gi

        if server_cpu <= 0 or server_mem_gi <= 0:
            log_error(
                f"nginx allocation ({nginx_cpu} vCPU, {nginx_mem_gi}Gi) exceeds "
                f"pod total ({res['cpu']} vCPU, {res['memory_gi']}Gi) — "
                f"reduce nginx.cpu/nginx.memory_gi or use a larger instance_type"
            )
            sys.exit(1)

        nginx_cpu_str = str(nginx_cpu)
        nginx_mem_str = f"{nginx_mem_gi}Gi"
        server_cpu_str = str(server_cpu)
        server_mem_str = f"{server_mem_gi}Gi"

        log_info(
            f"Dedicated nodes ({instance_type}): {res['cpu']} vCPU, "
            f"{res['memory_gi']}Gi per pod ({pods_per_node} pods/node)"
        )
        log_info(f"  nginx: {nginx_cpu} vCPU, {nginx_mem_gi}Gi — pypiserver: {server_cpu} vCPU, {server_mem_gi}Gi")

        # NVMe cache sizing
        nvme_cache_gi = compute_nginx_cache_size(instance_type, pods_per_node)
        if nvme_cache_gi:
            log_info(f"  nginx cache: {nvme_cache_gi}Gi per pod (NVMe hostPath)")

        # nodeSelector block — no leading indent on first line because the
        # template already has 6-space indent before __NODE_SELECTOR_BLOCK__
        node_selector_block = (
            f'nodeSelector:\n        workload-type: pypi-cache\n        instance-type: "{instance_type}"'
        )
        # Toleration entries (8-space indent for list items under tolerations:)
        tolerations_entries = (
            "        - key: workload\n"
            "          operator: Equal\n"
            '          value: "pypi-cache"\n'
            "          effect: NoSchedule"
        )
    else:
        # Shared base nodes: use manual config values
        server = config.get("server", DEFAULTS["server"])
        nginx_cpu_str = str(nginx_cfg["cpu"])
        nginx_mem_str = f"{nginx_cfg['memory_gi']}Gi"
        server_cpu_str = server["cpu"]
        server_mem_str = server["memory"]

        nvme_cache_gi = None
        node_selector_block = ""
        tolerations_entries = (
            "        - key: CriticalAddonsOnly\n          operator: Exists\n          effect: NoSchedule"
        )

    docs = []
    for slug in slugs:
        content = template_path.read_text()

        # NVMe: hostPath volume with per-slug directory, init container for chown
        # Non-NVMe: emptyDir volume, no init container
        if nvme_cache_gi:
            nginx_cache_volume = (
                f"        - name: nginx-cache\n"
                f"          hostPath:\n"
                f"            path: /mnt/k8s-disks/0/nginx-cache-{slug}\n"
                f"            type: DirectoryOrCreate"
            )
            init_nvme_block = (
                "        - name: init-nginx-cache\n"
                "          image: busybox:1.36\n"
                '          command: ["chown", "-R", "65534:65534", "/var/cache/nginx"]\n'
                "          securityContext:\n"
                "            runAsNonRoot: false\n"
                "            runAsUser: 0\n"
                "          volumeMounts:\n"
                "            - name: nginx-cache\n"
                "              mountPath: /var/cache/nginx\n"
            )
        else:
            nginx_cache_volume = (
                f"        - name: nginx-cache\n          emptyDir:\n            sizeLimit: {nginx_cfg['cache_size']}"
            )
            init_nvme_block = ""

        content = content.replace("__NAMESPACE__", config["namespace"])
        content = content.replace("__CUDA_SLUG__", slug)
        content = content.replace("__REPLICAS__", str(config["replicas"]))
        content = content.replace("__IMAGE__", config["image"])
        content = content.replace("__NGINX_IMAGE__", config["nginx_image"])
        content = content.replace("__INTERNAL_PORT__", str(config["internal_port"]))
        content = content.replace("__WORKERS__", str(config["workers"]))
        content = content.replace("__NGINX_CPU__", nginx_cpu_str)
        content = content.replace("__NGINX_MEMORY__", nginx_mem_str)
        content = content.replace("__SERVER_CPU__", server_cpu_str)
        content = content.replace("__SERVER_MEMORY__", server_mem_str)
        content = content.replace("__NODE_SELECTOR_BLOCK__", node_selector_block)
        content = content.replace("__TOLERATIONS_ENTRIES__", tolerations_entries)
        content = content.replace("__NGINX_CACHE_VOLUME__", nginx_cache_volume)
        # Remove __INIT_NVME_BLOCK__ line entirely when empty, preserve content when set
        if init_nvme_block:
            content = content.replace("__INIT_NVME_BLOCK__", init_nvme_block)
        else:
            content = content.replace("__INIT_NVME_BLOCK__\n", "")
        docs.append(content)
    return "\n---\n".join(docs)


def generate_services(config: dict, template_path: Path) -> str:
    """Generate Service manifests for all CUDA slugs.

    Returns a multi-document YAML string with --- separators.
    """
    slugs = get_slugs(config)
    docs = []
    for slug in slugs:
        content = template_path.read_text()
        content = content.replace("__NAMESPACE__", config["namespace"])
        content = content.replace("__CUDA_SLUG__", slug)
        content = content.replace("__SERVER_PORT__", str(config["server_port"]))
        docs.append(content)
    return "\n---\n".join(docs)


def main():
    parser = argparse.ArgumentParser(description="Generate pypi-cache Kubernetes manifests")
    parser.add_argument("--cluster", required=True, help="Cluster ID from clusters.yaml")
    parser.add_argument("--clusters-yaml", required=True, help="Path to clusters.yaml")
    parser.add_argument("--efs-filesystem-id", help="EFS filesystem ID from terraform output")
    parser.add_argument("--output-dir", help="Directory to write generated YAML files")
    parser.add_argument("--instance-type", help="Override instance type (empty = use config)")
    parser.add_argument(
        "--list-slugs",
        action="store_true",
        help="Print CUDA slugs only (one per line), then exit",
    )
    parser.add_argument(
        "--print-nginx-max-cache-size",
        action="store_true",
        help="Print nginx max_size value (e.g. '601g') and exit",
    )
    args = parser.parse_args()

    clusters_yaml = Path(args.clusters_yaml)
    config = load_config(clusters_yaml, args.cluster)

    # CLI --instance-type overrides config
    if args.instance_type is not None:
        config["instance_type"] = args.instance_type

    if args.list_slugs:
        for slug in get_slugs(config):
            print(slug)
        return 0

    if args.print_nginx_max_cache_size:
        instance_type = config.get("instance_type") or ""
        if instance_type:
            slugs = get_slugs(config)
            nvme_cache_gi = compute_nginx_cache_size(instance_type, len(slugs))
            if nvme_cache_gi:
                print(f"{nvme_cache_gi}g")
                return 0
        # Fallback: derive from emptyDir cache_size (e.g. "30Gi" -> "25g")
        nginx_cfg = config.get("nginx", DEFAULTS["nginx"])
        cache_gi = int(nginx_cfg["cache_size"].rstrip("Gi"))
        print(f"{cache_gi - 5}g")
        return 0

    if not args.efs_filesystem_id:
        parser.error("--efs-filesystem-id is required when not using --list-slugs")
    if not args.output_dir:
        parser.error("--output-dir is required when not using --list-slugs")

    # Template directory: scripts/python/ -> scripts/ -> pypi-cache/ -> kubernetes/
    template_dir = Path(__file__).resolve().parent.parent.parent / "kubernetes"

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    instance_type = config.get("instance_type") or ""
    log_info(f"Generating pypi-cache manifests for cluster '{args.cluster}'")
    log_info(f"  slugs: {', '.join(get_slugs(config))}")
    log_info(f"  replicas: {config['replicas']}, image: {config['image']}")
    if instance_type:
        log_info(f"  instance_type: {instance_type} (dedicated nodes)")

    # StorageClass
    sc_path = output_dir / "storageclass.yaml"
    sc_path.write_text(generate_storageclass(config, template_dir / "storageclass.yaml.tpl", args.efs_filesystem_id))
    print(sc_path)

    # PVC
    pvc_path = output_dir / "pvc.yaml"
    pvc_path.write_text(generate_pvc(config, template_dir / "pvc.yaml.tpl"))
    print(pvc_path)

    # Deployments
    deploy_path = output_dir / "deployments.yaml"
    deploy_path.write_text(generate_deployments(config, template_dir / "deployment.yaml.tpl"))
    print(deploy_path)

    # Services
    svc_path = output_dir / "services.yaml"
    svc_path.write_text(generate_services(config, template_dir / "service.yaml.tpl"))
    print(svc_path)

    # NodePools (only when instance_type is configured)
    if instance_type:
        nodepools_path = output_dir / "nodepools.yaml"
        nodepools_path.write_text(generate_nodepools(config, template_dir))
        print(nodepools_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
