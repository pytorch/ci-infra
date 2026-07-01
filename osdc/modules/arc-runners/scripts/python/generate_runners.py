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

# instance_specs and fleet_naming live in scripts/python/ at the repo root.
# Add it to sys.path so the import works both when run directly (deploy.sh)
# and when run via pytest (pyproject.toml testpaths also adds it).
_scripts_python = str(Path(__file__).resolve().parents[4] / "scripts" / "python")
if _scripts_python not in sys.path:
    sys.path.insert(0, _scripts_python)

from conditional_blocks import strip_conditional_block  # noqa: E402
from fleet_naming import derive_fleet_name  # noqa: E402
from nodepool_defs import load_excluded_instance_types  # noqa: E402
from runner_fleet_validator import validate_cluster_runner_fleets  # noqa: E402

# ANSI colors
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
NC = "\033[0m"


def log_info(msg):
    print(f"{GREEN}\u2192{NC} {msg}")


def log_warning(msg):
    print(f"{YELLOW}\u26a0{NC} {msg}", file=sys.stderr)


def log_error(msg):
    print(f"{RED}\u2717{NC} {msg}")


def normalize_name(name):
    """Normalize runner name for K8s resources (replace dots and underscores with dashes)."""
    return name.replace(".", "-").replace("_", "-")


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


def compute_cluster_sharding(clusters_yaml, cluster_id, module_name, runner_name_prefix):
    """Return (cluster_index, cluster_count) for sharding HUD queue across peer clusters.

    Peers are clusters that deploy the same module AND use the same
    runner_name_prefix — together they advertise overlapping runner labels and
    must shard the queue. The index is the cluster's position in the
    alphabetically-sorted peer list; the count is the size of that list.

    A cluster that is not its own peer (configuration drift, called with a
    module/prefix combination it does not actually deploy) returns (0, 1) so
    the listener degrades to single-cluster behavior instead of mis-sharding.
    """
    target_prefix = runner_name_prefix or ""
    peers = sorted(
        cid
        for cid, cfg in (clusters_yaml.get("clusters") or {}).items()
        if module_name in (cfg.get("modules") or [])
        and (((cfg.get("arc-runners") or {}).get("runner_name_prefix")) or "") == target_prefix
    )
    if cluster_id not in peers:
        return 0, 1
    return peers.index(cluster_id), len(peers)


def _resolve_consumer_root(upstream_dir):
    env_root = os.environ.get("OSDC_ROOT", "")
    if not env_root:
        return None
    candidate = Path(env_root).resolve()
    if not candidate.is_dir():
        return None
    if candidate == upstream_dir.resolve():
        return None
    return candidate


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


def resolve_max_runners(value, def_file, cluster_id):
    """Resolve a runner def's max_runners field to a concrete int or None.

    Accepts:
      - None: returns None (caller decides fallback, e.g., MAX_INT32 or chart default).
      - A positive int (>= 1): returned as-is.
      - A dict {"default": <int>, <cluster_id>: <int>, ...}: resolved via
        value.get(cluster_id, value["default"]). The "default" key is required;
        every dict value must be a positive int.

    Raises ValueError on invalid input. `def_file` is used in error messages.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        if "default" not in value:
            raise ValueError(
                f"Invalid definition file {def_file}: max_runners mapping must include a "
                f"`default` key for the baseline value, got keys {sorted(value)}"
            )
        for cid, v in value.items():
            if not isinstance(v, int) or v < 1:
                raise ValueError(
                    f"Invalid definition file {def_file}: max_runners[{cid!r}] must be a positive integer, got {v!r}"
                )
        return value.get(cluster_id, value["default"])
    if not isinstance(value, int) or value < 1:
        raise ValueError(
            f"Invalid definition file {def_file}: max_runners must be a positive integer or "
            f"a mapping with a `default` key, got {value!r}"
        )
    return value


def generate_runner(
    def_file,
    template_content,
    cluster_config,
    output_dir,
    module_name,
    pypi_cache_enabled=True,
    hf_cache_enabled=False,
    available_modules=None,
    cluster_cfg=None,
):
    """Generate a single runner config from its definition.

    pypi_cache_enabled controls whether the `# BEGIN_PYPI_CACHE` / `# END_PYPI_CACHE`
    block in the template is preserved (True) or stripped (False). Strip when the
    cluster does not deploy the pypi-cache module — the env vars would otherwise
    point at a Service that doesn't exist on this cluster.

    hf_cache_enabled does the same for the `# BEGIN_HF_CACHE` / `# END_HF_CACHE`
    block (HF_HOME env + the read-only /mnt/hf_cache hostPath mount). Strip when the
    cluster does not deploy the hf-cache module — the hostPath would otherwise be
    empty and HF_HUB_OFFLINE=1 would make every model load fail.

    available_modules is the set of modules the cluster deploys. A resolved
    scheduler_name that does not name an available module is silently dropped,
    so workflow pods don't get stamped with a schedulerName that has no
    scheduler running (they would pend forever).
    """
    available_modules = available_modules or set()
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
    # Optional concurrency cap for fixed-capacity pools (e.g. B200 Capacity Blocks).
    # Omit to leave the RSS unbounded, which is correct for Karpenter-managed pools
    # that can scale out on demand.
    max_runners = runner.get("max_runners")
    proactive_capacity = runner.get("proactive_capacity", 0)
    max_burst_capacity = runner.get("max_burst_capacity", 0)
    hud_failure_base_capacity = runner.get("hud_failure_base_capacity", 0)
    node_fleet_override = runner.get("node_fleet")
    proactive_cap = cluster_config.get("proactive_capacity_max")
    if proactive_cap is not None:
        proactive_capacity = min(proactive_capacity, proactive_cap)

    fresh_multiplier = runner.get("fresh_multiplier")
    if fresh_multiplier is None:
        module_block = (cluster_cfg or {}).get(module_name) or {}
        fresh_multiplier = module_block.get("capacity_aware_fresh_multiplier", 1.0)
    try:
        fresh_multiplier = float(fresh_multiplier)
    except (ValueError, TypeError):
        log_error(f"Invalid fresh_multiplier for runner {runner_name}: {fresh_multiplier!r} (must be a number)")
        sys.exit(1)

    if not runner_name or not instance_type:
        log_error(f"Invalid definition file: {def_file}")
        return False

    max_runners = resolve_max_runners(runner.get("max_runners"), def_file, cluster_config.get("cluster_id"))

    if cluster_config.get("pause_runners"):
        max_runners = 0
        hud_failure_base_capacity = 0

    # Region-exclusion guard: if the backing nodepool/fleet excludes the
    # cluster's region (no underlying capacity), force advertised capacity to
    # zero so GitHub does not route jobs that would pend forever.
    if instance_type in (cluster_config.get("excluded_instance_types") or set()):
        log_warning(
            f"{runner_name}: instance_type {instance_type} excluded in region "
            f"{cluster_config.get('region', '?')} via modules/nodepools/defs/ "
            f"— forcing max_runners=0, proactive_capacity=0, hud_failure_base_capacity=0"
        )
        max_runners = 0
        proactive_capacity = 0
        hud_failure_base_capacity = 0

    if max_burst_capacity is not None and (not isinstance(max_burst_capacity, int) or max_burst_capacity < 0):
        log_error(
            f"Invalid definition file {def_file}: max_burst_capacity must be a non-negative integer, "
            f"got {max_burst_capacity!r}"
        )
        return False

    if max_burst_capacity > 0 and proactive_capacity > 0 and max_burst_capacity < proactive_capacity:
        log_error(
            f"In {def_file}: max_burst_capacity ({max_burst_capacity}) < proactive_capacity ({proactive_capacity}); "
            f"the cap will prevent the listener from reaching its proactive baseline. "
            f"Either raise max_burst_capacity or lower proactive_capacity."
        )
        return False

    if max_burst_capacity > 0 and hud_failure_base_capacity > 0 and max_burst_capacity < hud_failure_base_capacity:
        log_error(
            f"In {def_file}: max_burst_capacity ({max_burst_capacity}) < hud_failure_base_capacity ({hud_failure_base_capacity}); "
            f"the cap will prevent the listener from reaching its HUD-fallback baseline. "
            f"Either raise max_burst_capacity or lower hud_failure_base_capacity."
        )
        return False

    try:
        node_fleet = derive_fleet_name(instance_type, override=node_fleet_override)
    except ValueError as e:
        log_error(f"Invalid definition file {def_file}: {e}")
        return False

    # Cluster-specific values
    github_url = cluster_config.get("github_config_url", "")

    # Cluster-level runner_group override (e.g. multi-region prod assigns a
    # per-region runner group so two clusters can share scale-set names without
    # collision). When set, wins over the def file's value. The repo-scope
    # guard below still applies.
    cluster_runner_group = cluster_config.get("runner_group")
    if cluster_runner_group:
        runner_group = cluster_runner_group

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
        gpu_job_tolerations = ""
        gpu_node_selector_affinity = ""
        gpu_request = ""
        gpu_limit = ""

    # Runner class isolation snippets (workflow pod only)
    # Release runners: required affinity to target release nodes
    # Regular runners: required affinity to avoid release nodes (DoesNotExist)
    if runner_class:
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
        runner_class_job_affinity = (
            "          requiredDuringSchedulingIgnoredDuringExecution:\n"
            "            nodeSelectorTerms:\n"
            "              - matchExpressions:\n"
            "                  - key: osdc.io/runner-class\n"
            "                    operator: DoesNotExist\n"
        )

    # Optional maxRunners line — only emitted when max_runners is set in the def
    max_runners_line = f"maxRunners: {max_runners}" if max_runners is not None else ""

    # Optional schedulerName for workflow pods (per-def scheduler_name).
    # Empty = default scheduler. The same value feeds two places so the real
    # workflow pod and its capacity placeholder (ph-w-*) agree on the scheduler:
    #   {{SCHEDULER_NAME_LINE}} -> schedulerName on the workflow pod spec
    #   {{SCHEDULER_NAME}}      -> CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME on the
    #                             listener, which the fork stamps onto ph-w-*.
    # If they diverged, the placeholder would reserve a slot the real pod can't claim.
    scheduler_name = runner.get("scheduler_name", "")
    # Cluster-wide default: a def without its own scheduler_name inherits
    # arc-runners.scheduler_name from clusters.yaml, so a cluster can route all
    # workflow pods to a secondary scheduler from one place (per-def value wins).
    if not scheduler_name:
        scheduler_name = cluster_config.get("scheduler_name", "")
    # Convention: module dir name == scheduler name. If the cluster does not
    # deploy a module with this name, silently drop — otherwise the workflow pod
    # would be stamped with a schedulerName that has no scheduler running.
    if scheduler_name and scheduler_name not in available_modules:
        scheduler_name = ""
    scheduler_name_line = f"      schedulerName: {scheduler_name}" if scheduler_name else ""

    # Replace all template placeholders
    output_content = template_content
    output_content = strip_conditional_block(output_content, "PYPI_CACHE", keep=pypi_cache_enabled)
    output_content = strip_conditional_block(output_content, "HF_CACHE", keep=hf_cache_enabled)
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
        "{{GPU_JOB_TOLERATIONS}}": gpu_job_tolerations,
        "{{GPU_NODE_SELECTOR_AFFINITY}}": gpu_node_selector_affinity,
        "{{GPU_REQUEST}}": gpu_request,
        "{{GPU_LIMIT}}": gpu_limit,
        "{{MODULE_NAME}}": module_name,
        "{{RUNNER_IMAGE}}": runner_image,
        # HF cache write target (used by the ci-refresh-hf-cache workflow's OIDC
        # upload); read path is the /mnt/hf_cache mount. Bucket is per-cluster.
        "{{HF_CACHE_BUCKET}}": f"pytorch-hf-model-cache-{cluster_config.get('cluster_id', '')}",
        "{{HF_CACHE_REGION}}": cluster_config.get("region", ""),
        "{{RUNNER_GROUP}}": runner_group,
        "{{RUNNER_CLASS_JOB_AFFINITY}}": runner_class_job_affinity,
        "{{MAX_RUNNERS_LINE}}": max_runners_line,
        "{{GPU_COUNT}}": str(gpu),
        "{{RUNNER_CLASS}}": runner_class,
        "{{PROACTIVE_CAPACITY}}": str(proactive_capacity),
        "{{MAX_BURST_CAPACITY}}": str(max_burst_capacity),
        "{{HUD_FAILURE_BASE_CAPACITY}}": str(hud_failure_base_capacity),
        "{{SCHEDULER_NAME_LINE}}": scheduler_name_line,
        "{{SCHEDULER_NAME}}": scheduler_name,
        "{{CAPACITY_AWARE_CLUSTER_INDEX}}": str(cluster_config.get("capacity_aware_cluster_index", 0)),
        "{{CAPACITY_AWARE_CLUSTER_COUNT}}": str(cluster_config.get("capacity_aware_cluster_count", 1)),
        "{{CAPACITY_AWARE_AGE_THRESHOLD_SECONDS}}": str(
            cluster_config.get("capacity_aware_age_threshold_seconds", 900)
        ),
        "{{CAPACITY_AWARE_FRESH_MULTIPLIER}}": str(fresh_multiplier),
    }

    for placeholder, value in replacements.items():
        output_content = output_content.replace(placeholder, value)

    # Collapse runs of 3+ newlines (from empty placeholder lines) into a single blank line
    while "\n\n\n" in output_content:
        output_content = output_content.replace("\n\n\n", "\n\n")

    output_file = output_dir / f"{runner_name}.yaml"
    output_file.write_text(output_content)  # lgtm[py/clear-text-storage-sensitive-data]
    log_info(f"  \u2713 {output_file.name}")
    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: generate_runners.py <cluster-id>")
        print()
        print("Example: generate_runners.py meta-staging-aws-uw1")
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

    cluster_config["runner_image"] = os.environ["RUNNER_IMAGE"]

    proactive_cap = cluster_cfg.get("proactive_capacity_max")
    if proactive_cap is not None and (
        isinstance(proactive_cap, bool) or not isinstance(proactive_cap, int) or proactive_cap < 0
    ):
        log_error(
            f"proactive_capacity_max for cluster '{cluster_id}' must be a non-negative integer, got {proactive_cap!r}"
        )
        return 1
    cluster_config["proactive_capacity_max"] = proactive_cap
    if proactive_cap is not None:
        log_warning(
            f"proactive_capacity_max={proactive_cap} for cluster '{cluster_id}' — "
            f"proactive_capacity capped at {proactive_cap} for all scale sets"
        )

    cluster_config["pause_runners"] = bool(cluster_cfg.get("pause_runners"))
    if cluster_config["pause_runners"]:
        log_warning(f"pause_runners=true for cluster '{cluster_id}' — all scale sets will render with maxRunners: 0")

    # Mirror the nodepools generator's exclude_regions handling: zero out
    # advertised capacity for any runner whose instance_type is in a fleet/
    # nodepool def that excludes this cluster's region.
    region = cluster_cfg.get("region", "")
    nodepools_defs_dir = (
        Path(os.environ["NODEPOOLS_DEFS_DIR"])
        if "NODEPOOLS_DEFS_DIR" in os.environ
        else repo_root / "modules" / "nodepools" / "defs"
    )
    cluster_config["region"] = region
    cluster_config["excluded_instance_types"] = load_excluded_instance_types(nodepools_defs_dir, region)
    if cluster_config["excluded_instance_types"]:
        log_info(
            f"Region {region}: nodepool exclude_regions hits {len(cluster_config['excluded_instance_types'])} "
            f"instance type(s); matching runners will render with maxRunners: 0"
        )

    # Stash the cluster id so generate_runner() can pick the right
    # max_runners_overrides entry from each def.
    cluster_config["cluster_id"] = cluster_id
    prefix = cluster_config.get("runner_name_prefix") or ""
    shard_idx, shard_count = compute_cluster_sharding(clusters_yaml, cluster_id, module_name, prefix)
    cluster_config["capacity_aware_cluster_index"] = shard_idx
    cluster_config["capacity_aware_cluster_count"] = shard_count
    cluster_config["capacity_aware_age_threshold_seconds"] = cluster_cfg.get(
        "capacity_aware_age_threshold_seconds", 900
    )

    # Hard-fail before touching the output dir: a runner pointing at a fleet no
    # NodePool defines would pend forever at apply time, and wiping generated/
    # before validation would destroy the previous (good) render on failure.
    consumer_root = _resolve_consumer_root(repo_root)
    fleet_errors = validate_cluster_runner_fleets(
        cluster_id,
        clusters_yaml,
        upstream_dir=repo_root,
        consumer_root=consumer_root,
    )
    if fleet_errors:
        for err in fleet_errors:
            log_error(err)
        sys.exit(1)

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

    # pypi-cache module is cluster-scoped: when absent, strip the pypi-cache env
    # vars from the workflow pod template so jobs don't try to reach a Service
    # that doesn't exist on this cluster.
    pypi_cache_enabled = "pypi-cache" in (cluster_cfg.get("modules") or [])
    available_modules = set(cluster_cfg.get("modules") or [])

    # hf-cache module is cluster-scoped: when absent, strip the HF_CACHE env vars
    # and the /mnt/hf_cache hostPath mount from the workflow pod template so jobs
    # don't mount an empty path and fail offline model loads.
    hf_cache_enabled = "hf-cache" in (cluster_cfg.get("modules") or [])

    count = 0
    for def_file in def_files:
        if generate_runner(
            def_file,
            template_content,
            cluster_config,
            output_dir,
            module_name,
            pypi_cache_enabled,
            hf_cache_enabled,
            available_modules,
            cluster_cfg=cluster_cfg,
        ):
            count += 1

    print()
    log_info(f"Generated {count} ARC runner config(s) in {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
