#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Generate ARC runner scale set configs from runner definitions + template.

Reads:  modules/arc-runners/defs/*.yaml + templates/runner.yaml.tpl + clusters.yaml
Writes: modules/arc-runners/generated/*.yaml (one per runner definition)

Each generated YAML is a multi-document file:
  - Document 1: Helm values for gha-runner-scale-set chart
  - Document 2: ConfigMap with job pod hook template

All per-installation config (GitHub URL, secret, prefix)
comes from clusters.yaml — no separate env-values.yaml.
"""

import os
import shutil
import sys
from pathlib import Path

import yaml

# instance_specs lives in scripts/python/ at the repo root.  Add it to
# sys.path so the import works both when run directly (deploy.sh) and
# when run via pytest (pyproject.toml testpaths also adds it).
_scripts_python = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _scripts_python not in sys.path:
    sys.path.insert(0, _scripts_python)

from instance_specs import INSTANCE_SPECS  # noqa: E402

# ANSI colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
NC = "\033[0m"


def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")


def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}")


def normalize_name(name):
    """Normalize runner name for K8s resources (replace dots and underscores with dashes)."""
    return name.replace(".", "-").replace("_", "-")


def derive_fleet_name(instance_type):
    """Derive the node-fleet name from an instance type.

    Uses the instance's actual GPU count from INSTANCE_SPECS rather than the
    runner's requested GPU count, since multiple runners with different GPU
    requests may share the same multi-GPU instance (e.g. B200).
    """
    if instance_type not in INSTANCE_SPECS:
        raise ValueError(
            f"Instance type '{instance_type}' not found in INSTANCE_SPECS. "
            f"Add it to scripts/python/instance_specs.py before using it."
        )
    family = instance_type.split(".")[0]  # r7a, g5, c7i, p6-b200, etc.
    node_gpus = INSTANCE_SPECS[instance_type].get("gpu", 0)
    if node_gpus:
        return f"{family}-{node_gpus}gpu"
    return family


# Kubernetes resource quantity suffixes → multiplier (bytes)
_K8S_MEMORY_SUFFIXES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_memory_bytes(memory_str):
    """Convert a Kubernetes memory quantity string to bytes.

    Supports binary (Ki, Mi, Gi, Ti) and decimal (K, M, G, T) suffixes,
    as well as plain integer strings (already in bytes).

    >>> parse_memory_bytes("115Gi")
    123480309760
    >>> parse_memory_bytes("512Mi")
    536870912
    >>> parse_memory_bytes("1024")
    1024
    """
    s = str(memory_str)
    # Try two-char suffix first (Ki, Mi, Gi, Ti), then one-char (K, M, G, T)
    for suffix_len in (2, 1):
        if len(s) > suffix_len:
            suffix = s[-suffix_len:]
            if suffix in _K8S_MEMORY_SUFFIXES:
                return int(s[:-suffix_len]) * _K8S_MEMORY_SUFFIXES[suffix]
    return int(s)


def load_clusters_yaml(repo_root):
    """Load clusters.yaml from the repository root."""
    config_path = repo_root / "clusters.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_cluster_config(clusters_yaml, cluster_id):
    """Get cluster config with defaults applied."""
    defaults = clusters_yaml.get("defaults", {})
    clusters = clusters_yaml.get("clusters", {})

    if cluster_id not in clusters:
        return None, None

    cluster_cfg = clusters[cluster_id]
    return cluster_cfg, defaults


def resolve_value(cluster_cfg, defaults, dotpath):
    """Resolve a dot-separated path against cluster config with defaults fallback."""
    parts = dotpath.split(".")
    val = cluster_cfg
    dval = defaults
    for part in parts:
        val = val.get(part) if isinstance(val, dict) else None
        dval = dval.get(part) if isinstance(dval, dict) else None
    if val is not None:
        return val
    return dval


def generate_runner(def_file, template_content, cluster_config, output_dir, module_name):
    """Generate a single runner config from its definition."""
    with open(def_file) as f:
        data = yaml.safe_load(f)

    runner = data.get("runner", {})
    runner_name = runner.get("name")
    instance_type = runner.get("instance_type")
    vcpu = runner.get("vcpu")
    memory = runner.get("memory")
    gpu = runner.get("gpu", 0)
    disk_size = runner.get("disk_size", 100)
    runner_group = runner.get("runner_group", "default")
    runner_class = runner.get("runner_class", "")

    if not runner_name or not instance_type:
        log_error(f"Invalid definition file: {def_file}")
        return False

    node_fleet = derive_fleet_name(instance_type)

    # Cluster-specific values
    github_url = cluster_config.get("github_config_url", "")

    # Runner groups are an org-level GitHub concept. Repo-scoped githubConfigUrl
    # (e.g. github.com/org/repo) can't resolve runner groups — force "default".
    if runner_group != "default" and "github.com/" in github_url:
        url_path = github_url.rstrip("/").split("github.com/", 1)[-1]
        if "/" in url_path:
            log_info(f"  Repo-scoped URL — overriding runner_group '{runner_group}' → 'default'")
            runner_group = "default"
    k8s_secret_ref = cluster_config.get(
        "github_secret_name", ""
    )  # lgtm[py/clear-text-storage-sensitive-data] - K8s Secret resource name, not a credential
    runner_prefix = cluster_config.get("runner_name_prefix", "")
    runner_image = cluster_config.get("runner_image", "ghcr.io/actions/actions-runner:2.333.1")

    normalized_name = normalize_name(runner_name)

    log_info(
        f"Generating {runner_prefix}{runner_name} "
        f"({instance_type}, {vcpu} vCPU, {memory} RAM, {gpu} GPU, "
        f"{disk_size}Gi disk)"
    )

    # GPU-specific YAML snippets
    if gpu > 0:
        gpu_tolerations = """
      - key: nvidia.com/gpu
        operator: Equal
        value: "true"
        effect: NoSchedule"""
        gpu_job_tolerations = """
        - key: nvidia.com/gpu
          operator: Equal
          value: "true"
          effect: NoSchedule"""
        # Affinity-style GPU selector for job pod's preferredDuringScheduling
        gpu_node_selector_affinity = """
                  - key: nvidia.com/gpu
                    operator: In
                    values:
                      - "true\""""
        gpu_request = f'\n              nvidia.com/gpu: "{gpu}"'
        gpu_limit = f'\n              nvidia.com/gpu: "{gpu}"'
    else:
        gpu_tolerations = ""
        gpu_job_tolerations = ""
        gpu_node_selector_affinity = ""
        gpu_request = ""
        gpu_limit = ""

    # Runner class isolation snippets
    # Release runners: use nodeSelector to target release nodes
    # Regular runners: use anti-affinity to avoid release nodes
    if runner_class:
        runner_class_node_selector = f"      osdc.io/runner-class: {runner_class}\n"
        runner_class_affinity = ""
        runner_class_job_affinity = (
            "          requiredDuringSchedulingIgnoredDuringExecution:\n"
            "            nodeSelectorTerms:\n"
            "              - matchExpressions:\n"
            "                  - key: osdc.io/runner-class\n"
            "                    operator: In\n"
            "                    values:\n"
            f'                      - "{runner_class}"\n'
        )
    else:
        runner_class_node_selector = ""
        runner_class_affinity = (
            "    affinity:\n"
            "      nodeAffinity:\n"
            "        requiredDuringSchedulingIgnoredDuringExecution:\n"
            "          nodeSelectorTerms:\n"
            "            - matchExpressions:\n"
            "                - key: osdc.io/runner-class\n"
            "                  operator: DoesNotExist\n"
        )
        runner_class_job_affinity = (
            "          requiredDuringSchedulingIgnoredDuringExecution:\n"
            "            nodeSelectorTerms:\n"
            "              - matchExpressions:\n"
            "                  - key: osdc.io/runner-class\n"
            "                    operator: DoesNotExist\n"
        )

    # Replace all template placeholders
    output_content = template_content
    replacements = {
        "{{GITHUB_CONFIG_URL}}": github_url,
        "{{GITHUB_SECRET_NAME}}": k8s_secret_ref,
        "{{RUNNER_NAME_PREFIX}}": runner_prefix,
        "{{RUNNER_NAME}}": runner_name,
        "{{RUNNER_NAME_NORMALIZED}}": normalized_name,
        "{{INSTANCE_TYPE}}": instance_type,
        "{{NODE_FLEET}}": node_fleet,
        "{{VCPU}}": str(vcpu),
        "{{MEMORY}}": str(memory),
        "{{MEMORY_BYTES}}": str(parse_memory_bytes(memory)),
        "{{DISK_SIZE}}": f"{disk_size}Gi",
        "{{GPU_TOLERATIONS}}": gpu_tolerations,
        "{{GPU_JOB_TOLERATIONS}}": gpu_job_tolerations,
        "{{GPU_NODE_SELECTOR_AFFINITY}}": gpu_node_selector_affinity,
        "{{GPU_REQUEST}}": gpu_request,
        "{{GPU_LIMIT}}": gpu_limit,
        "{{MODULE_NAME}}": module_name,
        "{{RUNNER_IMAGE}}": runner_image,
        "{{RUNNER_GROUP}}": runner_group,
        "{{RUNNER_CLASS_NODE_SELECTOR}}": runner_class_node_selector,
        "{{RUNNER_CLASS_AFFINITY}}": runner_class_affinity,
        "{{RUNNER_CLASS_JOB_AFFINITY}}": runner_class_job_affinity,
    }

    for placeholder, value in replacements.items():
        output_content = output_content.replace(placeholder, value)

    output_file = output_dir / f"{runner_name}.yaml"
    output_file.write_text(output_content)  # lgtm[py/clear-text-storage-sensitive-data]
    log_info(f"  \u2713 {output_file.name}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: generate_runners.py <cluster-id>")
        print()
        print("Example: generate_runners.py arc-staging")
        return 1

    cluster_id = sys.argv[1]

    script_dir = Path(__file__).parent
    module_dir = script_dir.parent.parent
    repo_root = Path(os.environ["OSDC_ROOT"]) if "OSDC_ROOT" in os.environ else module_dir.parent.parent
    defs_dir = Path(os.environ["ARC_RUNNERS_DEFS_DIR"]) if "ARC_RUNNERS_DEFS_DIR" in os.environ else module_dir / "defs"
    template_file = (
        Path(os.environ["ARC_RUNNERS_TEMPLATE"])
        if "ARC_RUNNERS_TEMPLATE" in os.environ
        else module_dir / "templates" / "runner.yaml.tpl"
    )
    output_dir = (
        Path(os.environ["ARC_RUNNERS_OUTPUT_DIR"])
        if "ARC_RUNNERS_OUTPUT_DIR" in os.environ
        else module_dir / "generated"
    )
    module_name = os.environ.get("ARC_RUNNERS_MODULE_NAME", "arc-runners")

    if not template_file.exists():
        log_error(f"Template not found: {template_file}")
        return 1

    # Load cluster config from clusters.yaml
    clusters_yaml = load_clusters_yaml(repo_root)
    cluster_cfg, defaults = get_cluster_config(clusters_yaml, cluster_id)

    if cluster_cfg is None:
        log_error(f"Unknown cluster '{cluster_id}' in clusters.yaml")
        return 1

    # Get arc-runners config for this cluster (with defaults fallback)
    cluster_config = resolve_value(cluster_cfg, defaults, "arc-runners") or {}
    if not cluster_config.get("github_config_url"):
        log_error(f"No 'arc-runners.github_config_url' configured for cluster '{cluster_id}' in clusters.yaml")
        return 1

    # Resolve runner image tag from arc.runner_image_tag (shared with arc module)
    runner_image_tag = resolve_value(cluster_cfg, defaults, "arc.runner_image_tag") or "2.333.1"
    cluster_config["runner_image"] = f"ghcr.io/actions/actions-runner:{runner_image_tag}"

    # Clean output dir so removed defs don't leave stale generated files
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir()

    template_content = template_file.read_text()

    log_info(f"Generating ARC runner configs for: {cluster_id}")
    print()

    def_files = sorted(defs_dir.glob("*.yaml"))
    if not def_files:
        log_error(f"No definition files found in {defs_dir}")
        return 1

    count = 0
    for def_file in def_files:
        if generate_runner(def_file, template_content, cluster_config, output_dir, module_name):
            count += 1

    print()
    log_info(f"Generated {count} ARC runner config(s) in {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
