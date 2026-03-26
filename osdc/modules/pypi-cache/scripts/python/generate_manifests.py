#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Generate pypi-cache Kubernetes manifests from clusters.yaml config.

Reads cluster configuration and Kubernetes templates, substitutes placeholders,
and outputs manifests for the Deployment+EFS architecture (one Deployment per
CUDA version plus CPU).

Outputs:
  storageclass.yaml   — EFS-backed StorageClass
  pvc.yaml            — Shared PersistentVolumeClaim
  deployments.yaml    — Multi-doc YAML (one Deployment per CUDA slug)
  services.yaml       — Multi-doc YAML (one Service per CUDA slug)
"""

import argparse
import copy
import sys
from pathlib import Path

import yaml

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"

DEFAULTS = {
    "namespace": "pypi-cache",
    "server_port": 8080,
    "image": "pypiserver/pypiserver:v2.4.1",
    "cuda_versions": ["12.1", "12.4"],
    "storage_request": "1Ti",
    "replicas": 2,
    "log_max_age_days": 30,
    "server": {
        "cpu_request": "100m",
        "cpu_limit": "500m",
        "memory_request": "256Mi",
        "memory_limit": "512Mi",
    },
}


def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")


def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}", file=sys.stderr)


def cuda_slug(version: str) -> str:
    """Convert CUDA version to slug: '12.1' -> 'cu121', '11.8' -> 'cu118'."""
    return "cu" + version.replace(".", "")


def get_slugs(config: dict) -> list[str]:
    """Return list of all CUDA slugs including 'cpu'.

    Always starts with 'cpu', then each cuda_version converted to slug.
    """
    slugs = ["cpu"]
    for ver in config.get("cuda_versions", DEFAULTS["cuda_versions"]):
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


def generate_deployments(config: dict, template_path: Path) -> str:
    """Generate Deployment manifests for all CUDA slugs.

    Returns a multi-document YAML string with --- separators.
    """
    slugs = get_slugs(config)
    server = config.get("server", DEFAULTS["server"])
    docs = []
    for slug in slugs:
        content = template_path.read_text()
        content = content.replace("__NAMESPACE__", config["namespace"])
        content = content.replace("__CUDA_SLUG__", slug)
        content = content.replace("__REPLICAS__", str(config["replicas"]))
        content = content.replace("__IMAGE__", config["image"])
        content = content.replace("__SERVER_PORT__", str(config["server_port"]))
        content = content.replace("__LOG_MAX_AGE_DAYS__", str(config["log_max_age_days"]))
        content = content.replace("__CPU_REQUEST__", server["cpu_request"])
        content = content.replace("__CPU_LIMIT__", server["cpu_limit"])
        content = content.replace("__MEMORY_REQUEST__", server["memory_request"])
        content = content.replace("__MEMORY_LIMIT__", server["memory_limit"])
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
    parser.add_argument(
        "--list-slugs",
        action="store_true",
        help="Print CUDA slugs only (one per line), then exit",
    )
    args = parser.parse_args()

    clusters_yaml = Path(args.clusters_yaml)
    config = load_config(clusters_yaml, args.cluster)

    if args.list_slugs:
        for slug in get_slugs(config):
            print(slug)
        return 0

    if not args.efs_filesystem_id:
        parser.error("--efs-filesystem-id is required when not using --list-slugs")
    if not args.output_dir:
        parser.error("--output-dir is required when not using --list-slugs")

    # Template directory: scripts/python/ -> scripts/ -> pypi-cache/ -> kubernetes/
    template_dir = Path(__file__).resolve().parent.parent.parent / "kubernetes"

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    log_info(f"Generating pypi-cache manifests for cluster '{args.cluster}'")
    log_info(f"  slugs: {', '.join(get_slugs(config))}")
    log_info(f"  replicas: {config['replicas']}, image: {config['image']}")

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

    return 0


if __name__ == "__main__":
    sys.exit(main())
