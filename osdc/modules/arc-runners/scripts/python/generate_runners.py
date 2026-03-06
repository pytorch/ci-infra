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
import sys
from pathlib import Path

import yaml

# ANSI colors
GREEN = '\033[0;32m'
RED = '\033[0;31m'
NC = '\033[0m'

def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")

def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}")


def normalize_name(name):
    """Normalize runner name for K8s resources (replace dots and underscores with dashes)."""
    return name.replace('.', '-').replace('_', '-')


def load_clusters_yaml(repo_root):
    """Load clusters.yaml from the repository root."""
    config_path = repo_root / 'clusters.yaml'
    with open(config_path) as f:
        return yaml.safe_load(f)


def get_cluster_config(clusters_yaml, cluster_id):
    """Get cluster config with defaults applied."""
    defaults = clusters_yaml.get('defaults', {})
    clusters = clusters_yaml.get('clusters', {})

    if cluster_id not in clusters:
        return None, None

    cluster_cfg = clusters[cluster_id]
    return cluster_cfg, defaults


def resolve_value(cluster_cfg, defaults, dotpath):
    """Resolve a dot-separated path against cluster config with defaults fallback."""
    parts = dotpath.split('.')
    val = cluster_cfg
    dval = defaults
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            val = None
        if isinstance(dval, dict):
            dval = dval.get(part)
        else:
            dval = None
    if val is not None:
        return val
    return dval


def generate_runner(def_file, template_content, cluster_config, output_dir):
    """Generate a single runner config from its definition."""
    with open(def_file) as f:
        data = yaml.safe_load(f)

    runner = data.get('runner', {})
    runner_name = runner.get('name')
    instance_type = runner.get('instance_type')
    vcpu = runner.get('vcpu')
    memory = runner.get('memory')
    gpu = runner.get('gpu', 0)
    disk_size = runner.get('disk_size', 100)

    if not runner_name or not instance_type:
        log_error(f"Invalid definition file: {def_file}")
        return False

    # Cluster-specific values
    github_url = cluster_config.get('github_config_url', '')
    k8s_secret_ref = cluster_config.get('github_secret_name', '')  # lgtm[py/clear-text-storage-sensitive-data] - K8s Secret resource name, not a credential
    runner_prefix = cluster_config.get('runner_name_prefix', '')

    normalized_name = normalize_name(runner_name)

    log_info(f"Generating {runner_prefix}{runner_name} "
             f"({instance_type}, {vcpu} vCPU, {memory} RAM, {gpu} GPU, "
             f"{disk_size}Gi disk)")

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
        gpu_node_selector = '\n        nvidia.com/gpu: "true"'
        gpu_request = f'\n              nvidia.com/gpu: "{gpu}"'
        gpu_limit = f'\n              nvidia.com/gpu: "{gpu}"'
    else:
        gpu_tolerations = ""
        gpu_job_tolerations = ""
        gpu_node_selector = ""
        gpu_request = ""
        gpu_limit = ""

    # Replace all template placeholders
    output_content = template_content
    replacements = {
        '{{GITHUB_CONFIG_URL}}': github_url,
        '{{GITHUB_SECRET_NAME}}': k8s_secret_ref,
        '{{RUNNER_NAME_PREFIX}}': runner_prefix,
        '{{RUNNER_NAME}}': runner_name,
        '{{RUNNER_NAME_NORMALIZED}}': normalized_name,
        '{{INSTANCE_TYPE}}': instance_type,
        '{{VCPU}}': str(vcpu),
        '{{MEMORY}}': str(memory),
        '{{DISK_SIZE}}': f"{disk_size}Gi",
        '{{GPU_TOLERATIONS}}': gpu_tolerations,
        '{{GPU_JOB_TOLERATIONS}}': gpu_job_tolerations,
        '{{GPU_NODE_SELECTOR}}': gpu_node_selector,
        '{{GPU_REQUEST}}': gpu_request,
        '{{GPU_LIMIT}}': gpu_limit,
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
    defs_dir = module_dir / 'defs'
    template_file = module_dir / 'templates' / 'runner.yaml.tpl'
    output_dir = module_dir / 'generated'

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
    cluster_config = resolve_value(cluster_cfg, defaults, 'arc-runners') or {}
    if not cluster_config.get('github_config_url'):
        log_error(f"No 'arc-runners.github_config_url' configured for cluster '{cluster_id}' in clusters.yaml")
        return 1

    output_dir.mkdir(exist_ok=True)

    template_content = template_file.read_text()

    log_info(f"Generating ARC runner configs for: {cluster_id}")
    print()

    def_files = sorted(defs_dir.glob('*.yaml'))
    if not def_files:
        log_error(f"No definition files found in {defs_dir}")
        return 1

    count = 0
    for def_file in def_files:
        if generate_runner(def_file, template_content, cluster_config, output_dir):
            count += 1

    print()
    log_info(f"Generated {count} ARC runner config(s) in {output_dir}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
