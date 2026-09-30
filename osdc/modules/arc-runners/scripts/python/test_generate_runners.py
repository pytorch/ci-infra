"""Tests for generate_runners.py."""

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fleet_naming import derive_fleet_name
from generate_runners import (
    _additional_org_errors,
    _cluster_serves_org,
    _load_runner_names,
    compute_cluster_sharding,
    generate_runner,
    get_cluster_config,
    load_clusters_yaml,
    load_excluded_instance_types,
    main,
    normalize_name,
    org_key_of,
    parse_memory_bytes,
    resolve_max_runners,
    resolve_value,
)

# ============================================================================
# Fixtures
# ============================================================================

# MINIMAL_TEMPLATE mirrors the real runner.yaml.tpl's structural shape: the
# runner pod is pinned to the dedicated c7i-runner pool (literal, not templated)
# and carries no GPU/runner-class wiring; all GPU and runner-class substitutions
# land on the workflow pod (ConfigMap) only. Tests below verify the substitution
# mechanism (placeholders -> values) on the workflow side. Structural invariants
# of the real template are covered separately by TestRealTemplate.
MINIMAL_TEMPLATE = textwrap.dedent("""\
    githubConfigUrl: "{{GITHUB_CONFIG_URL}}"
    githubConfigSecret: "{{GITHUB_SECRET_NAME}}"
    runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"
    minRunners: 0
    {{MAX_RUNNERS_LINE}}
    runnerGroup: "{{RUNNER_GROUP}}"
    listenerTemplate:
      spec:
        containers:
          - name: listener
            env:
              - name: CAPACITY_AWARE_PROACTIVE_CAPACITY
                value: "{{PROACTIVE_CAPACITY}}"
              - name: CAPACITY_AWARE_MAX_BURST_CAPACITY
                value: "{{MAX_BURST_CAPACITY}}"
              - name: CAPACITY_AWARE_HUD_FAILURE_BASE_CAPACITY
                value: "{{HUD_FAILURE_BASE_CAPACITY}}"
              - name: CAPACITY_AWARE_FRESH_MULTIPLIER
                value: "{{CAPACITY_AWARE_FRESH_MULTIPLIER}}"
              - name: CAPACITY_AWARE_AGED_MULTIPLIER
                value: "{{CAPACITY_AWARE_AGED_MULTIPLIER}}"
    template:
      spec:
        containers:
          - name: runner
            image: {{RUNNER_IMAGE}}
        nodeSelector:
          workload-type: github-runner
          node-fleet: "c7i-runner"
        tolerations:
          - key: node-fleet
            operator: Equal
            value: "c7i-runner"
            effect: NoSchedule
          - key: instance-type
            operator: Exists
            effect: NoSchedule
    ---
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: arc-runner-hook-{{RUNNER_NAME_NORMALIZED}}
      namespace: arc-runners
      labels:
        osdc.io/module: {{MODULE_NAME}}
    data:
      job-pod.yaml: |
        spec:
          affinity:
            nodeAffinity:
    {{RUNNER_CLASS_JOB_AFFINITY}}
              preferredDuringSchedulingIgnoredDuringExecution:
                - weight: 50
                  preference:
                    matchExpressions:
                      - key: node-fleet
                        operator: In
                        values:
                          - "{{NODE_FLEET}}"
                      - key: workload-type
                        operator: In
                        values:
                          - github-runner{{GPU_NODE_SELECTOR_AFFINITY}}
          tolerations:
            - key: node-fleet
              operator: Equal
              value: "{{NODE_FLEET}}"
              effect: NoSchedule
            - key: instance-type
              operator: Exists
              effect: NoSchedule{{GPU_JOB_TOLERATIONS}}
          containers:
            - name: "$job"
              env:
                - name: PIP_INDEX_URL
                  value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/whl/cpu/"
                - name: PIP_TRUSTED_HOST
                  value: "pypi-cache-cpu.pypi-cache.svc.cluster.local"
                - name: UV_DEFAULT_INDEX
                  value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/whl/cpu/"
                - name: UV_INSECURE_HOST
                  value: "pypi-cache-cpu.pypi-cache.svc.cluster.local:8080"
                - name: PIP_EXTRA_INDEX_URL
                  value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/"
                - name: UV_INDEX
                  value: "http://pypi-cache-cpu.pypi-cache.svc.cluster.local:8080/simple/"
                - name: UV_INDEX_STRATEGY
                  value: "unsafe-best-match"
                - name: TORCH_CI_MAX_MEMORY
                  value: "{{MEMORY_BYTES}}"
              resources:
                requests:
                  cpu: "{{VCPU}}"
                  memory: "{{MEMORY}}"
                  ephemeral-storage: "{{DISK_SIZE}}"{{GPU_REQUEST}}
                limits:
                  cpu: "{{VCPU}}"
                  memory: "{{MEMORY}}"
                  ephemeral-storage: "{{DISK_SIZE}}"{{GPU_LIMIT}}
              volumeMounts:
                - name: dshm
                  mountPath: /dev/shm
          volumes:
            - name: dshm
              emptyDir:
                medium: Memory
                sizeLimit: 2Gi
""")

# Path to the real runner.yaml.tpl shipped with the module. Tests in
# TestRealTemplate render against this file (not MINIMAL_TEMPLATE) so they
# defend invariants that exist only in the real template — runner pod pinned
# to c7i-runner, priorityClassName values, GPU references confined to the
# workflow side, etc.
REAL_TEMPLATE_PATH = Path(__file__).parent.parent.parent / "templates" / "runner.yaml.tpl"


@pytest.fixture(scope="module")
def real_template():
    """Read the real runner.yaml.tpl shipped with the module."""
    return REAL_TEMPLATE_PATH.read_text()


FAKE_CLUSTERS_YAML = {
    "defaults": {
        "arc-runners": {
            "github_config_url": "https://github.com/default-org",
            "github_secret_name": "default-secret",
            "runner_name_prefix": "default-",
        },
    },
    "clusters": {
        "staging": {
            "cluster_name": "my-staging",
            "region": "us-west-2",
            "modules": ["nodepools", "arc-runners"],
            "arc-runners": {
                "github_config_url": "https://github.com/test-org",
                "github_secret_name": "gh-secret",
                "runner_name_prefix": "staging-",
            },
        },
        "no-runners": {
            "cluster_name": "no-runners",
            "region": "us-east-1",
            "modules": [],
        },
    },
}


def make_def_file(
    tmp_path,
    name,
    instance_type,
    vcpu,
    memory,
    gpu=0,
    disk_size=100,
    runner_group=None,
    runner_class=None,
    max_runners=None,
    proactive_capacity=None,
    max_burst_capacity=None,
    hud_failure_base_capacity=None,
    node_fleet=None,
    scheduler_name=None,
    fresh_multiplier=None,
    imex_channels=None,
):
    """Write a runner def YAML and return the path.

    `max_runners` accepts either an int (baseline for every cluster) or a dict
    of `{default: int, <cluster_id>: int, ...}` matching the generator schema.
    """
    runner = {
        "name": name,
        "instance_type": instance_type,
        "vcpu": vcpu,
        "memory": f"{memory}Gi",
        "gpu": gpu,
        "disk_size": disk_size,
    }
    if runner_group is not None:
        runner["runner_group"] = runner_group
    if runner_class is not None:
        runner["runner_class"] = runner_class
    if max_runners is not None:
        runner["max_runners"] = max_runners
    if proactive_capacity is not None:
        runner["proactive_capacity"] = proactive_capacity
    if max_burst_capacity is not None:
        runner["max_burst_capacity"] = max_burst_capacity
    if hud_failure_base_capacity is not None:
        runner["hud_failure_base_capacity"] = hud_failure_base_capacity
    if node_fleet is not None:
        runner["node_fleet"] = node_fleet
    if scheduler_name is not None:
        runner["scheduler_name"] = scheduler_name
    if fresh_multiplier is not None:
        runner["fresh_multiplier"] = fresh_multiplier
    if imex_channels is not None:
        runner["imex_channels"] = imex_channels
    content = {"runner": runner}
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.dump(content, default_flow_style=False))
    return p


def make_nodepool_defs(osdc_root, instance_types, module="nodepools"):
    """Create nodepool fleet defs under OSDC_ROOT for the given instance types.

    Derives one fleet per unique instance-family prefix using fleet_naming so the
    validator can match runner defs to fleets without rewriting test fixtures
    every time fleet-naming rules change.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[4] / "scripts" / "python"))
    from fleet_naming import derive_fleet_name as _derive

    defs_dir = osdc_root / "modules" / module / "defs"
    defs_dir.mkdir(parents=True, exist_ok=True)
    by_fleet = {}
    for itype in instance_types:
        by_fleet.setdefault(_derive(itype), []).append(itype)
    for fleet_name, types in by_fleet.items():
        content = {
            "fleet": {
                "name": fleet_name,
                "arch": "amd64",
                "gpu": False,
                "instances": [{"type": t, "weight": 100, "node_disk_size": 600, "has_nvme": True} for t in types],
            }
        }
        (defs_dir / f"{fleet_name}.yaml").write_text(yaml.dump(content, default_flow_style=False))


def _org_entry(**overrides):
    """A minimal well-formed additional_orgs entry (the three required keys).

    Tests pass keyword overrides to make an individual field missing or invalid.
    """
    entry = {
        "github_config_url": "https://github.com/meta-pytorch",
        "github_secret_name": "mp-secret",
        "resource_slug": "mp",
    }
    entry.update(overrides)
    return entry


# ============================================================================
# normalize_name
# ============================================================================


class TestNormalizeName:
    def test_dots_replaced(self):
        assert normalize_name("a.b.c") == "a-b-c"

    def test_underscores_replaced(self):
        assert normalize_name("a_b_c") == "a-b-c"

    def test_mixed(self):
        assert normalize_name("x86.avx_512") == "x86-avx-512"

    def test_already_clean(self):
        assert normalize_name("l-x86iavx512-2-4") == "l-x86iavx512-2-4"

    def test_empty_string(self):
        assert normalize_name("") == ""


# ============================================================================
# derive_fleet_name
# ============================================================================


class TestDeriveFleetName:
    def test_derive_fleet_name_cpu(self):
        assert derive_fleet_name("r7a.48xlarge") == "r7a"

    def test_derive_fleet_name_gpu(self):
        assert derive_fleet_name("g5.8xlarge") == "g5"

    def test_derive_fleet_name_gpu_multi(self):
        assert derive_fleet_name("g5.48xlarge") == "g5"

    def test_derive_fleet_name_b200(self):
        assert derive_fleet_name("p6-b200.48xlarge") == "p6-b200"

    def test_derive_fleet_name_metal(self):
        assert derive_fleet_name("c7i.metal-24xl") == "c7i"

    def test_derive_fleet_name_unknown(self):
        assert derive_fleet_name("z99.xlarge") == "z99"

    def test_derive_fleet_name_override_overrides_family(self):
        assert derive_fleet_name("g5.48xlarge", override="g5-48xlarge") == "g5-48xlarge"

    def test_derive_fleet_name_override_none_falls_back(self):
        assert derive_fleet_name("g5.48xlarge", override=None) == "g5"

    def test_derive_fleet_name_override_empty_string_raises(self):
        with pytest.raises(ValueError, match="node_fleet override invalid"):
            derive_fleet_name("g5.48xlarge", override="")


# ============================================================================
# parse_memory_bytes
# ============================================================================


class TestParseMemoryBytes:
    def test_gibibytes(self):
        assert parse_memory_bytes("115Gi") == 115 * 1024**3

    def test_mebibytes(self):
        assert parse_memory_bytes("512Mi") == 512 * 1024**2

    def test_tebibytes(self):
        assert parse_memory_bytes("1Ti") == 1 * 1024**4

    def test_kibibytes(self):
        assert parse_memory_bytes("256Ki") == 256 * 1024

    def test_decimal_gigabytes(self):
        assert parse_memory_bytes("10G") == 10 * 1000**3

    def test_decimal_megabytes(self):
        assert parse_memory_bytes("500M") == 500 * 1000**2

    def test_plain_integer(self):
        assert parse_memory_bytes("1024") == 1024

    def test_integer_input(self):
        assert parse_memory_bytes(1024) == 1024

    def test_common_runner_values(self):
        """Verify against real runner def values."""
        assert parse_memory_bytes("4Gi") == 4 * 1024**3
        assert parse_memory_bytes("16Gi") == 16 * 1024**3
        assert parse_memory_bytes("768Gi") == 768 * 1024**3


# ============================================================================
# load_clusters_yaml
# ============================================================================


class TestLoadClustersYaml:
    def test_loads_valid_yaml(self, tmp_path):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))
        result = load_clusters_yaml(tmp_path)
        assert "clusters" in result
        assert "staging" in result["clusters"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_clusters_yaml(tmp_path)


# ============================================================================
# load_excluded_instance_types
# ============================================================================


class TestLoadExcludedInstanceTypes:
    def _write_def(self, dirpath, name, content):
        p = dirpath / f"{name}.yaml"
        p.write_text(yaml.dump(content, default_flow_style=False))

    def test_fleet_format_excludes_all_listed_instances(self, tmp_path):
        """A fleet def with exclude_regions returns every instance type it declares."""
        self._write_def(
            tmp_path,
            "g5",
            {
                "fleet": {
                    "name": "g5",
                    "exclude_regions": ["us-west-1"],
                    "instances": [{"type": "g5.48xlarge"}, {"type": "g5.12xlarge"}],
                }
            },
        )
        assert load_excluded_instance_types(tmp_path, "us-west-1") == {"g5.48xlarge", "g5.12xlarge"}

    def test_legacy_nodepool_format(self, tmp_path):
        """A legacy single-instance nodepool def returns its instance_type."""
        self._write_def(
            tmp_path,
            "p4d-24xlarge",
            {
                "nodepool": {
                    "name": "p4d-24xlarge",
                    "instance_type": "p4d.24xlarge",
                    "exclude_regions": ["us-west-1"],
                }
            },
        )
        assert load_excluded_instance_types(tmp_path, "us-west-1") == {"p4d.24xlarge"}

    def test_non_matching_region_returns_empty(self, tmp_path):
        """An exclude_regions list that does not contain the target region contributes nothing."""
        self._write_def(
            tmp_path,
            "g5",
            {
                "fleet": {
                    "name": "g5",
                    "exclude_regions": ["us-west-1"],
                    "instances": [{"type": "g5.48xlarge"}],
                }
            },
        )
        assert load_excluded_instance_types(tmp_path, "us-east-2") == set()

    def test_no_exclude_regions_returns_empty(self, tmp_path):
        """A def without exclude_regions contributes nothing."""
        self._write_def(
            tmp_path,
            "c7i",
            {"fleet": {"name": "c7i", "instances": [{"type": "c7i.24xlarge"}]}},
        )
        assert load_excluded_instance_types(tmp_path, "us-west-1") == set()

    def test_empty_region_returns_empty(self, tmp_path):
        """A falsy region short-circuits to an empty set (backward-compatible)."""
        self._write_def(
            tmp_path,
            "g5",
            {
                "fleet": {
                    "name": "g5",
                    "exclude_regions": ["us-west-1"],
                    "instances": [{"type": "g5.48xlarge"}],
                }
            },
        )
        assert load_excluded_instance_types(tmp_path, "") == set()

    def test_missing_dir_returns_empty(self, tmp_path):
        """A missing nodepools defs dir returns an empty set rather than raising."""
        assert load_excluded_instance_types(tmp_path / "absent", "us-west-1") == set()

    def test_multiple_defs_merge(self, tmp_path):
        """Excluded instance types from multiple defs are unioned."""
        self._write_def(
            tmp_path,
            "g5",
            {
                "fleet": {
                    "name": "g5",
                    "exclude_regions": ["us-west-1"],
                    "instances": [{"type": "g5.48xlarge"}],
                }
            },
        )
        self._write_def(
            tmp_path,
            "p4d-24xlarge",
            {
                "nodepool": {
                    "name": "p4d-24xlarge",
                    "instance_type": "p4d.24xlarge",
                    "exclude_regions": ["us-west-1"],
                }
            },
        )
        self._write_def(
            tmp_path,
            "c7i",
            {"fleet": {"name": "c7i", "instances": [{"type": "c7i.24xlarge"}]}},
        )
        assert load_excluded_instance_types(tmp_path, "us-west-1") == {"g5.48xlarge", "p4d.24xlarge"}


# ============================================================================
# get_cluster_config
# ============================================================================


class TestGetClusterConfig:
    def test_valid_cluster(self):
        cfg, defaults = get_cluster_config(FAKE_CLUSTERS_YAML, "staging")
        assert cfg is not None
        assert cfg["cluster_name"] == "my-staging"
        assert defaults is not None

    def test_missing_cluster(self):
        cfg, defaults = get_cluster_config(FAKE_CLUSTERS_YAML, "nonexistent")
        assert cfg is None
        assert defaults is None

    def test_defaults_returned(self):
        _, defaults = get_cluster_config(FAKE_CLUSTERS_YAML, "staging")
        assert "arc-runners" in defaults


# ============================================================================
# resolve_value
# ============================================================================


class TestResolveValue:
    def test_cluster_override(self):
        cluster_cfg = {"arc-runners": {"github_config_url": "https://override"}}
        defaults = {"arc-runners": {"github_config_url": "https://default"}}
        assert resolve_value(cluster_cfg, defaults, "arc-runners.github_config_url") == "https://override"

    def test_fallback_to_defaults(self):
        cluster_cfg = {}
        defaults = {"arc-runners": {"runner_name_prefix": "default-"}}
        assert resolve_value(cluster_cfg, defaults, "arc-runners.runner_name_prefix") == "default-"

    def test_missing_from_both(self):
        assert resolve_value({}, {}, "arc-runners.github_config_url") is None

    def test_nested_path(self):
        cluster_cfg = {"a": {"b": {"c": 42}}}
        assert resolve_value(cluster_cfg, {}, "a.b.c") == 42

    def test_non_dict_intermediate(self):
        cluster_cfg = {"arc-runners": "not-a-dict"}
        defaults = {"arc-runners": {"key": "val"}}
        assert resolve_value(cluster_cfg, defaults, "arc-runners.key") == "val"

    def test_top_level_key(self):
        cluster_cfg = {"region": "us-west-2"}
        defaults = {"region": "us-east-1"}
        assert resolve_value(cluster_cfg, defaults, "region") == "us-west-2"


# ============================================================================
# resolve_max_runners
# ============================================================================


class TestResolveMaxRunners:
    def test_none_returns_none(self):
        assert resolve_max_runners(None, Path("dummy"), "arc-x") is None

    def test_positive_int_passes_through(self):
        assert resolve_max_runners(7, Path("dummy"), "arc-x") == 7

    def test_mapping_picks_cluster_override(self):
        assert resolve_max_runners({"default": 1, "arc-x": 5}, Path("dummy"), "arc-x") == 5

    def test_mapping_falls_back_to_default(self):
        assert resolve_max_runners({"default": 1, "arc-x": 5}, Path("dummy"), "arc-y") == 1

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="max_runners must be a positive integer"):
            resolve_max_runners(0, Path("dummy"), "arc-x")

    def test_mapping_missing_default_raises(self):
        with pytest.raises(ValueError, match="max_runners mapping must include a `default` key"):
            resolve_max_runners({"arc-x": 5}, Path("dummy"), "arc-x")


# ============================================================================
# generate_runner
# ============================================================================


class TestGenerateRunner:
    def test_non_gpu_runner(self, tmp_path):
        def_file = make_def_file(tmp_path, "cpu-runner", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "staging-",
        }

        result = generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        assert result is True

        output_file = output_dir / "cpu-runner.yaml"
        assert output_file.exists()

        docs = list(yaml.safe_load_all(output_file.read_text()))
        assert len(docs) == 2

        # Helm values doc — top-level substitutions and the literal c7i-runner pin
        helm = docs[0]
        assert helm["githubConfigUrl"] == "https://github.com/test-org"
        assert helm["githubConfigSecret"] == "gh-secret"
        assert helm["runnerScaleSetName"] == "staging-cpu-runner"
        assert helm["template"]["spec"]["nodeSelector"]["node-fleet"] == "c7i-runner"

        # ConfigMap doc
        cm = docs[1]
        assert cm["kind"] == "ConfigMap"
        assert cm["metadata"]["name"] == "arc-runner-hook-cpu-runner"
        assert cm["metadata"]["labels"]["osdc.io/module"] == "arc-runners"

        # Workflow pod gets the def's fleet (m6i) via {{NODE_FLEET}} substitution.
        cm_data = yaml.safe_load(cm["data"]["job-pod.yaml"])
        assert "nodeSelector" not in cm_data["spec"]
        prefs = cm_data["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        assert len(prefs) == 1
        assert prefs[0]["weight"] == 50
        match_exprs = prefs[0]["preference"]["matchExpressions"]
        keys = [e["key"] for e in match_exprs]
        assert "node-fleet" in keys
        assert "workload-type" in keys
        assert "nvidia.com/gpu" not in keys  # CPU runner has no GPU affinity
        node_fleet_expr = next(e for e in match_exprs if e["key"] == "node-fleet")
        assert node_fleet_expr["values"] == ["m6i"]
        node_fleet_tols = [t for t in cm_data["spec"]["tolerations"] if t.get("key") == "node-fleet"]
        assert len(node_fleet_tols) == 1
        assert node_fleet_tols[0]["value"] == "m6i"

    def test_gpu_runner(self, tmp_path):
        """GPU substitutions land on the workflow pod (toleration + resources + affinity);
        the runner pod stays GPU-free since it's pinned to the c7i-runner pool.
        """
        def_file = make_def_file(tmp_path, "gpu-runner", "g4dn.12xlarge", 16, 64, gpu=1, disk_size=150)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "",
        }

        result = generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        assert result is True

        content = (output_dir / "gpu-runner.yaml").read_text()

        # GPU substitutions should appear somewhere in the rendered output.
        assert "nvidia.com/gpu" in content

        docs = list(yaml.safe_load_all(content))
        helm = docs[0]

        # Runner pod must NOT carry any GPU toleration — runner pods live on the
        # dedicated c7i-runner pool, GPU scheduling is workflow-side only.
        runner_taint_keys = [t["key"] for t in helm["template"]["spec"]["tolerations"]]
        assert "nvidia.com/gpu" not in runner_taint_keys

        # ConfigMap job-pod data should have GPU toleration, GPU resources, and GPU affinity.
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        workflow_taint_keys = [t["key"] for t in cm_data["spec"]["tolerations"]]
        assert "nvidia.com/gpu" in workflow_taint_keys

        container = cm_data["spec"]["containers"][0]
        assert container["resources"]["requests"]["nvidia.com/gpu"] == "1"
        assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"

        # Job pod affinity should include nvidia.com/gpu matchExpression
        assert "nodeSelector" not in cm_data["spec"]
        prefs = cm_data["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        assert len(prefs) == 1
        match_exprs = prefs[0]["preference"]["matchExpressions"]
        keys = [e["key"] for e in match_exprs]
        assert "node-fleet" in keys
        assert "workload-type" in keys
        assert "nvidia.com/gpu" in keys

        # /dev/shm tmpfs mount — 64Mi k8s default is too small for NCCL
        mounts = {m["name"]: m for m in container.get("volumeMounts", [])}
        assert mounts["dshm"]["mountPath"] == "/dev/shm"
        volumes = {v["name"]: v for v in cm_data["spec"].get("volumes", [])}
        assert volumes["dshm"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "2Gi"}

    def test_cpu_runner_has_dshm_mount(self, tmp_path):
        def_file = make_def_file(tmp_path, "cpu-runner", "c5.xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "cpu-runner.yaml").read_text()))
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        mounts = {m["name"]: m for m in container.get("volumeMounts", [])}
        assert mounts["dshm"]["mountPath"] == "/dev/shm"
        volumes = {v["name"]: v for v in cm_data["spec"].get("volumes", [])}
        assert volumes["dshm"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "2Gi"}

    def test_no_placeholders_remaining(self, tmp_path, real_template):
        """Renderer must substitute every placeholder in the real template."""
        def_file = make_def_file(tmp_path, "test-runner", "c7i.24xlarge", 2, 4)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/org",
            "github_secret_name": "secret",
            "runner_name_prefix": "pre-",
        }

        generate_runner(def_file, real_template, cluster_config, output_dir, "arc-runners")
        content = (output_dir / "test-runner.yaml").read_text()
        assert "{{" not in content
        assert "}}" not in content

    def test_normalized_name_in_output(self, tmp_path):
        def_file = make_def_file(tmp_path, "runner.with_dots", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/org",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        content = (output_dir / "runner.with_dots.yaml").read_text()
        docs = list(yaml.safe_load_all(content))
        # ConfigMap name should use normalized name
        assert docs[1]["metadata"]["name"] == "arc-runner-hook-runner-with-dots"

    def test_invalid_def_no_name(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump({"runner": {"instance_type": "m6i.32xlarge"}}, default_flow_style=False))
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        result = generate_runner(p, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners")
        assert result is False

    def test_invalid_def_no_instance_type(self, tmp_path):
        p = tmp_path / "bad2.yaml"
        p.write_text(yaml.dump({"runner": {"name": "test"}}, default_flow_style=False))
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        result = generate_runner(p, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners")
        assert result is False

    def test_max_runners_omitted_by_default(self, tmp_path):
        """Without max_runners in the def, the RSS stays unbounded (no maxRunners key)."""
        def_file = make_def_file(tmp_path, "elastic-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "elastic-runner.yaml").read_text()))
        assert docs[0]["minRunners"] == 0
        assert "maxRunners" not in docs[0]

    def test_max_runners_applied_when_set(self, tmp_path):
        """max_runners: N in the def emits maxRunners: N in the helm values."""
        def_file = make_def_file(tmp_path, "fixed-runner", "p6-b200.48xlarge", 22, 225, gpu=1, max_runners=8)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "fixed-runner.yaml").read_text()))
        assert docs[0]["minRunners"] == 0
        assert docs[0]["maxRunners"] == 8

    def test_max_runners_mapping_picks_per_cluster_value(self, tmp_path):
        """A per-cluster entry in the max_runners mapping replaces the default baseline."""
        def_file = make_def_file(
            tmp_path,
            "h100-1gpu",
            "p5.48xlarge",
            22,
            225,
            gpu=1,
            max_runners={"default": 8, "meta-prod-aws-uw1": 48},
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "cluster_id": "meta-prod-aws-uw1",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners-h100") is True

        docs = list(yaml.safe_load_all((output_dir / "h100-1gpu.yaml").read_text()))
        assert docs[0]["maxRunners"] == 48

    def test_max_runners_mapping_falls_back_to_default(self, tmp_path):
        """When the cluster has no explicit entry, the `default` baseline applies."""
        def_file = make_def_file(
            tmp_path,
            "h100-1gpu",
            "p5.48xlarge",
            22,
            225,
            gpu=1,
            max_runners={"default": 8, "meta-prod-aws-uw1": 48},
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "cluster_id": "meta-prod-aws-ue2",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners-h100") is True

        docs = list(yaml.safe_load_all((output_dir / "h100-1gpu.yaml").read_text()))
        assert docs[0]["maxRunners"] == 8

    def test_max_runners_mapping_rejects_missing_default(self, tmp_path):
        """A max_runners mapping without a `default` key is rejected."""
        def_file = make_def_file(
            tmp_path,
            "h100-1gpu",
            "p5.48xlarge",
            22,
            225,
            gpu=1,
            max_runners={"meta-prod-aws-uw1": 48},
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "cluster_id": "meta-prod-aws-uw1",
        }

        with pytest.raises(ValueError, match="max_runners mapping must include a `default` key"):
            generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners-h100")

    def test_pause_runners_overrides_max_runners_mapping(self, tmp_path):
        """pause_runners=true wins over a per-cluster mapping entry — maxRunners is forced to 0."""
        def_file = make_def_file(
            tmp_path,
            "h100-1gpu",
            "p5.48xlarge",
            22,
            225,
            gpu=1,
            max_runners={"default": 8, "meta-prod-aws-uw1": 48},
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "cluster_id": "meta-prod-aws-uw1",
            "pause_runners": True,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners-h100") is True

        docs = list(yaml.safe_load_all((output_dir / "h100-1gpu.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0

    def test_region_exclusion_overrides_max_runners_mapping(self, tmp_path):
        """A region-excluded instance type forces maxRunners=0 even when a per-cluster entry is set."""
        def_file = make_def_file(
            tmp_path,
            "h100-1gpu",
            "p5.48xlarge",
            22,
            225,
            gpu=1,
            max_runners={"default": 8, "meta-prod-aws-uw1": 48},
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "cluster_id": "meta-prod-aws-uw1",
            "excluded_instance_types": {"p5.48xlarge"},
            "region": "us-west-1",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners-h100") is True

        docs = list(yaml.safe_load_all((output_dir / "h100-1gpu.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0

    def test_invalid_max_runners_zero(self, tmp_path):
        """max_runners must be a positive integer; 0 is rejected."""
        def_file = make_def_file(tmp_path, "bad-cap", "c7i.24xlarge", 4, 16, max_runners=0)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="max_runners must be a positive integer"):
            generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners")

    def test_invalid_max_runners_non_int(self, tmp_path):
        """max_runners must be an int, not a string."""
        def_file = make_def_file(tmp_path, "bad-cap2", "c7i.24xlarge", 4, 16, max_runners="8")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with pytest.raises(ValueError, match="max_runners must be a positive integer"):
            generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners")

    def test_pause_runners_forces_max_runners_zero_when_def_unset(self, tmp_path):
        """pause_runners=true forces maxRunners: 0 even when the def omits max_runners."""
        def_file = make_def_file(tmp_path, "elastic-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "pause_runners": True,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "elastic-runner.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0

    def test_pause_runners_overrides_def_max_runners(self, tmp_path):
        """pause_runners=true overrides a def-level max_runners value."""
        def_file = make_def_file(tmp_path, "fixed-runner", "p6-b200.48xlarge", 22, 225, gpu=1, max_runners=8)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "pause_runners": True,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "fixed-runner.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0

    def test_pause_runners_false_preserves_def_value(self, tmp_path):
        """pause_runners=false preserves the def's max_runners value."""
        def_file = make_def_file(tmp_path, "kept-runner", "c7i.24xlarge", 4, 16, max_runners=8)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "pause_runners": False,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "kept-runner.yaml").read_text()))
        assert docs[0]["maxRunners"] == 8

    def test_region_excluded_instance_forces_max_runners_zero(self, tmp_path):
        """Runners whose instance_type matches a nodepool exclude_regions entry render with maxRunners: 0."""
        def_file = make_def_file(
            tmp_path, "uw1-a10g", "g5.48xlarge", 189, 704, gpu=8, proactive_capacity=5, max_burst_capacity=30
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "region": "us-west-1",
            "excluded_instance_types": {"g5.48xlarge", "g5.12xlarge"},
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "uw1-a10g.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "0"

    def test_region_excluded_instance_overrides_def_max_runners(self, tmp_path):
        """A def-level max_runners is overridden when the instance_type is region-excluded."""
        def_file = make_def_file(tmp_path, "uw1-fixed", "p4d.24xlarge", 88, 1000, gpu=8, max_runners=4)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "region": "us-west-1",
            "excluded_instance_types": {"p4d.24xlarge"},
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "uw1-fixed.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0

    def test_region_not_excluded_preserves_capacity(self, tmp_path):
        """When the instance_type is not in the excluded set, capacity is unchanged."""
        def_file = make_def_file(
            tmp_path, "ue2-a10g", "g5.48xlarge", 189, 704, gpu=8, proactive_capacity=5, max_burst_capacity=30
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "region": "us-east-2",
            "excluded_instance_types": set(),
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "ue2-a10g.yaml").read_text()))
        assert "maxRunners" not in docs[0]
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "5"

    def test_pause_runners_unset_preserves_unbounded(self, tmp_path):
        """pause_runners unset leaves the RSS unbounded when the def also omits max_runners."""
        def_file = make_def_file(tmp_path, "elastic-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "elastic-runner.yaml").read_text()))
        assert "maxRunners" not in docs[0]

    def test_proactive_capacity_default_zero(self, tmp_path):
        """proactive_capacity defaults to 0 when not in the runner def."""
        def_file = make_def_file(tmp_path, "cap-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "cap-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "0"

    def test_proactive_capacity_nonzero(self, tmp_path):
        """proactive_capacity: 30 renders as "30" in the listener env."""
        def_file = make_def_file(tmp_path, "warm-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "warm-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "30"

    def test_proactive_capacity_capped_to_zero(self, tmp_path):
        """proactive_capacity_max=0 clamps any def value down to 0."""
        def_file = make_def_file(tmp_path, "forced-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "proactive_capacity_max": 0,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "forced-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "0"

    def test_proactive_capacity_uncapped(self, tmp_path):
        """No proactive_capacity_max preserves the def value."""
        def_file = make_def_file(tmp_path, "kept-runner", "c7i.24xlarge", 4, 16, proactive_capacity=10)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "kept-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "10"

    def test_hud_failure_base_capacity_default_zero(self, tmp_path):
        """hud_failure_base_capacity defaults to 0 when not in the runner def."""
        def_file = make_def_file(tmp_path, "hud-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "hud-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_HUD_FAILURE_BASE_CAPACITY"] == "0"

    def test_hud_failure_base_capacity_nonzero(self, tmp_path):
        """hud_failure_base_capacity: 25 renders as "25" in the listener env."""
        def_file = make_def_file(tmp_path, "hud-warm-runner", "c7i.24xlarge", 4, 16, hud_failure_base_capacity=25)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "hud-warm-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_HUD_FAILURE_BASE_CAPACITY"] == "25"

    def test_proactive_capacity_capped_below_def(self, tmp_path):
        """proactive_capacity_max clamps a def value down to the cap."""
        def_file = make_def_file(tmp_path, "capped-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "proactive_capacity_max": 5,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "capped-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "5"

    def test_proactive_capacity_cap_above_def_noop(self, tmp_path):
        """proactive_capacity_max higher than the def value is a no-op."""
        def_file = make_def_file(tmp_path, "noop-runner", "c7i.24xlarge", 4, 16, proactive_capacity=10)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "proactive_capacity_max": 50,
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "noop-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "10"

    def test_max_burst_capacity_default_zero(self, tmp_path):
        """max_burst_capacity defaults to 0 when not in the runner def."""
        def_file = make_def_file(tmp_path, "burst-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "burst-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_MAX_BURST_CAPACITY"] == "0"

    def test_max_burst_capacity_nonzero(self, tmp_path):
        """max_burst_capacity: 50 renders as "50" in the listener env."""
        def_file = make_def_file(tmp_path, "capped-runner", "c7i.24xlarge", 4, 16, max_burst_capacity=50)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "capped-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_MAX_BURST_CAPACITY"] == "50"

    def test_invalid_max_burst_capacity_negative(self, tmp_path):
        """max_burst_capacity must be a non-negative integer; -1 is rejected."""
        def_file = make_def_file(tmp_path, "bad-burst", "c7i.24xlarge", 4, 16, max_burst_capacity=-1)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False

    def test_invalid_max_burst_capacity_non_int(self, tmp_path):
        """max_burst_capacity must be an int, not a string."""
        def_file = make_def_file(tmp_path, "bad-burst2", "c7i.24xlarge", 4, 16, max_burst_capacity="50")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False

    def test_invalid_fresh_multiplier_non_numeric(self, tmp_path, capsys):
        def_file = make_def_file(tmp_path, "bad-fresh", "c7i.24xlarge", 4, 16, fresh_multiplier="abc")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        with pytest.raises(SystemExit) as exc:
            generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners")
        assert exc.value.code == 1
        combined = capsys.readouterr().out + capsys.readouterr().err
        assert "fresh_multiplier" in combined or "bad-fresh" in combined

    def test_invalid_node_fleet_non_string(self, tmp_path, capsys):
        """node_fleet must be a string, not an int."""
        def_file = make_def_file(tmp_path, "bad-fleet-int", "c7i.24xlarge", 4, 16, node_fleet=123)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-int" in combined

    def test_invalid_node_fleet_empty_string(self, tmp_path, capsys):
        """node_fleet must be non-empty."""
        def_file = make_def_file(tmp_path, "bad-fleet-empty", "c7i.24xlarge", 4, 16, node_fleet="")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-empty" in combined

    def test_invalid_node_fleet_whitespace(self, tmp_path, capsys):
        """node_fleet must have no leading/trailing whitespace."""
        def_file = make_def_file(tmp_path, "bad-fleet-ws", "c7i.24xlarge", 4, 16, node_fleet="  g5-48xlarge  ")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-ws" in combined

    def test_invalid_node_fleet_embedded_newline(self, tmp_path, capsys):
        """node_fleet must not contain embedded newlines (YAML injection vector)."""
        def_file = make_def_file(tmp_path, "bad-fleet-nl", "c7i.24xlarge", 4, 16, node_fleet="g5\nevil")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-nl" in combined

    def test_invalid_node_fleet_embedded_quote(self, tmp_path, capsys):
        """node_fleet must not contain embedded quotes (YAML injection vector)."""
        def_file = make_def_file(tmp_path, "bad-fleet-q", "c7i.24xlarge", 4, 16, node_fleet='g5"evil')
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-q" in combined

    def test_invalid_node_fleet_uppercase(self, tmp_path, capsys):
        """node_fleet must be lowercase (DNS-1123 label)."""
        def_file = make_def_file(tmp_path, "bad-fleet-uc", "c7i.24xlarge", 4, 16, node_fleet="G5-48xlarge")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-uc" in combined

    def test_invalid_node_fleet_too_long(self, tmp_path, capsys):
        """node_fleet must be at most 63 chars (DNS-1123 label limit)."""
        def_file = make_def_file(tmp_path, "bad-fleet-long", "c7i.24xlarge", 4, 16, node_fleet="a" * 64)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-long" in combined

    def test_invalid_node_fleet_slash(self, tmp_path, capsys):
        """node_fleet must not contain slashes (DNS-1123 label)."""
        def_file = make_def_file(tmp_path, "bad-fleet-slash", "c7i.24xlarge", 4, 16, node_fleet="g5/48")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-slash" in combined

    def test_invalid_node_fleet_reserved_c7i_runner(self, tmp_path, capsys):
        """node_fleet must not be 'c7i-runner' (reserved for runner control-plane pool)."""
        def_file = make_def_file(tmp_path, "bad-fleet-reserved", "c7i.24xlarge", 4, 16, node_fleet="c7i-runner")
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, MINIMAL_TEMPLATE, {}, output_dir, "arc-runners") is False
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "node_fleet" in combined
        assert "bad-fleet-reserved" in combined
        assert "reserved" in combined

    def test_max_burst_capacity_zero_allowed(self, tmp_path):
        """max_burst_capacity: 0 is valid (means uncapped / disabled)."""
        def_file = make_def_file(tmp_path, "zero-burst", "c7i.24xlarge", 4, 16, max_burst_capacity=0)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "zero-burst.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_MAX_BURST_CAPACITY"] == "0"

    def test_max_burst_capacity_error_when_below_proactive(self, tmp_path, capsys):
        """Error when max_burst_capacity (>0) is less than proactive_capacity — the cap
        would prevent the listener from reaching its proactive baseline.
        """
        def_file = make_def_file(
            tmp_path, "misconfig-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30, max_burst_capacity=10
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is False

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "max_burst_capacity" in combined
        assert "proactive_capacity" in combined
        assert "10" in combined
        assert "30" in combined

    def test_max_burst_capacity_no_warning_when_above_proactive(self, tmp_path, capsys):
        """No warning when max_burst_capacity >= proactive_capacity."""
        def_file = make_def_file(
            tmp_path, "ok-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30, max_burst_capacity=100
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # The warning text mentions both fields together — plain mentions in "Generating ..."
        # don't trigger this assertion since neither field name appears in info logs.
        assert "max_burst_capacity" not in combined

    def test_max_burst_capacity_no_warning_when_proactive_zero(self, tmp_path, capsys):
        """No warning when proactive_capacity is 0, regardless of max_burst_capacity."""
        def_file = make_def_file(
            tmp_path, "noproactive-runner", "c7i.24xlarge", 4, 16, proactive_capacity=0, max_burst_capacity=5
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "max_burst_capacity" not in combined

    def test_max_burst_capacity_no_warning_when_burst_zero(self, tmp_path, capsys):
        """No warning when max_burst_capacity is 0 (uncapped), regardless of proactive."""
        def_file = make_def_file(
            tmp_path, "uncapped-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30, max_burst_capacity=0
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is True

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "max_burst_capacity" not in combined

    def test_max_burst_capacity_less_than_hud_failure_base_capacity_errors(self, tmp_path, capsys):
        """Error when max_burst_capacity (>0) is less than hud_failure_base_capacity — the cap
        would prevent the listener from reaching its HUD-fallback baseline.
        """
        def_file = make_def_file(
            tmp_path,
            "hud-misconfig-runner",
            "c7i.24xlarge",
            4,
            16,
            hud_failure_base_capacity=25,
            max_burst_capacity=10,
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners") is False

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "max_burst_capacity" in combined
        assert "hud_failure_base_capacity" in combined
        assert "10" in combined
        assert "25" in combined

    def test_resource_values_match_def(self, tmp_path):
        def_file = make_def_file(tmp_path, "res-test", "c7i.24xlarge", 48, 96, disk_size=200)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "res-test.yaml").read_text()))

        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        assert container["resources"]["requests"]["cpu"] == "48"
        assert container["resources"]["requests"]["memory"] == "96Gi"
        assert container["resources"]["requests"]["ephemeral-storage"] == "200Gi"

    def test_memory_bytes_env_var(self, tmp_path):
        """TORCH_CI_MAX_MEMORY env var contains memory in bytes."""
        def_file = make_def_file(tmp_path, "mem-test", "c7i.24xlarge", 48, 96, disk_size=200)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "mem-test.yaml").read_text()))

        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        env_vars = {e["name"]: e["value"] for e in container["env"]}
        assert "TORCH_CI_MAX_MEMORY" in env_vars
        assert env_vars["TORCH_CI_MAX_MEMORY"] == str(96 * 1024**3)

    def test_pypi_cache_env_vars(self, tmp_path):
        """All pypi-cache env vars are present with correct CPU defaults."""
        def_file = make_def_file(tmp_path, "cache-test", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "cache-test.yaml").read_text()))

        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        env_vars = {e["name"]: e["value"] for e in container["env"]}

        host = "pypi-cache-cpu.pypi-cache.svc.cluster.local"
        base = f"http://{host}:8080"

        assert env_vars["PIP_INDEX_URL"] == f"{base}/whl/cpu/"
        assert env_vars["PIP_TRUSTED_HOST"] == host
        assert env_vars["PIP_EXTRA_INDEX_URL"] == f"{base}/simple/"
        assert env_vars["UV_DEFAULT_INDEX"] == f"{base}/whl/cpu/"
        assert env_vars["UV_INSECURE_HOST"] == f"{host}:8080"
        assert env_vars["UV_INDEX"] == f"{base}/simple/"
        assert env_vars["UV_INDEX_STRATEGY"] == "unsafe-best-match"

    def test_runner_group_default(self, tmp_path):
        """Runner group defaults to 'default' when not specified."""
        def_file = make_def_file(tmp_path, "grp-test", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "grp-test.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "default"

    def test_runner_group_custom(self, tmp_path):
        """Runner group can be overridden in the def (org-scoped URL)."""
        def_file = make_def_file(tmp_path, "rel-grp", "m6i.32xlarge", 4, 16, runner_group="release-runners")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "rel-grp.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "release-runners"

    def test_runner_group_repo_scoped_override(self, tmp_path):
        """Runner group forced to 'default' when githubConfigUrl is repo-scoped."""
        def_file = make_def_file(tmp_path, "rel-repo", "m6i.32xlarge", 4, 16, runner_group="release-runners")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch/pytorch-canary",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "rel-repo.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "default"

    def test_runner_group_cluster_override_wins(self, tmp_path):
        """Cluster-level runner_group overrides the def file's value."""
        # Def file says "release-runners"...
        def_file = make_def_file(tmp_path, "ovr-def", "m6i.32xlarge", 4, 16, runner_group="release-runners")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        # ...but the cluster config says "meta-prod-aws-uw1" — cluster wins.
        cluster_config = {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "runner_group": "meta-prod-aws-uw1",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "ovr-def.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "meta-prod-aws-uw1"

    def test_runner_group_cluster_override_no_def_value(self, tmp_path):
        """Cluster-level runner_group applies even when the def doesn't set one."""
        def_file = make_def_file(tmp_path, "ovr-nodef", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "runner_group": "meta-prod-aws-uw1",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "ovr-nodef.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "meta-prod-aws-uw1"

    def test_runner_group_cluster_override_repo_scope_guard_still_wins(self, tmp_path):
        """Repo-scoped URL still forces 'default' even when cluster sets a custom group."""
        def_file = make_def_file(tmp_path, "ovr-repo", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch/pytorch-canary",  # repo-scoped
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "runner_group": "arc-staging-uw2",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "ovr-repo.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "default"

    def test_release_runner_group_gets_per_region_suffix(self, tmp_path):
        """A release-class runner lands in the cluster's per-region release group
        ({cluster_group}-release-runners), not the shared CI group."""
        def_file = make_def_file(
            tmp_path, "rel-suffix", "r7a.48xlarge", 8, 64, runner_group="default", runner_class="release"
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "runner_group": "meta-staging-aws-ue1",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "rel-suffix.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "meta-staging-aws-ue1-release-runners"

    def test_release_runner_group_no_cluster_group_is_default(self, tmp_path):
        """A release runner on a group-less cluster falls back to 'default' — no
        '-release-runners' with nothing in front, no double-suffix."""
        def_file = make_def_file(
            tmp_path, "rel-nogrp", "r7a.48xlarge", 8, 64, runner_group="default", runner_class="release"
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "rel-nogrp.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "default"

    def test_non_release_runner_keeps_cluster_group_unchanged(self, tmp_path):
        """CI (non-release) runners stay in the cluster's base group — the release
        suffix must not leak onto them."""
        def_file = make_def_file(tmp_path, "ci-runner", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
            "runner_group": "meta-staging-aws-ue1",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "ci-runner.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "meta-staging-aws-ue1"

    def test_runner_class_release(self, tmp_path):
        """Release runners get required runner-class affinity on the workflow pod;
        runner-class isolation does not apply to the runner pod (it lives on the
        dedicated c7i-runner pool, which carries no runner-class label).
        """
        def_file = make_def_file(
            tmp_path, "rel-runner", "r7a.48xlarge", 8, 64, runner_group="release-runners", runner_class="release"
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "rel-runner.yaml").read_text()))
        helm = docs[0]

        # Runner pod stays pinned to c7i-runner — no runner-class wiring.
        assert "osdc.io/runner-class" not in helm["template"]["spec"]["nodeSelector"]

        # Job pod should have required affinity for runner-class=release
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        required = cm_data["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        match_exprs = required["nodeSelectorTerms"][0]["matchExpressions"]
        keys = {e["key"]: e for e in match_exprs}
        assert "osdc.io/runner-class" in keys
        assert keys["osdc.io/runner-class"]["operator"] == "In"
        assert keys["osdc.io/runner-class"]["values"] == ["release"]

    def test_regular_runner_anti_affinity(self, tmp_path):
        """Regular (non-release) runners get DoesNotExist anti-affinity on the
        workflow pod so workflow scheduling avoids release nodes; the runner pod
        itself stays GPU/runner-class-free on the c7i-runner pool.
        """
        def_file = make_def_file(tmp_path, "reg-runner", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "reg-runner.yaml").read_text()))
        helm = docs[0]

        # Runner pod must not gain runner-class wiring (no nodeSelector key, no
        # affinity block) — the c7i-runner pool is shared across all runners.
        assert "osdc.io/runner-class" not in helm["template"]["spec"]["nodeSelector"]
        assert "affinity" not in helm["template"]["spec"]

        # Job pod should have DoesNotExist required affinity to avoid release nodes.
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        job_required = cm_data["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        job_exprs = job_required["nodeSelectorTerms"][0]["matchExpressions"]
        job_keys = {e["key"]: e for e in job_exprs}
        assert "osdc.io/runner-class" in job_keys
        assert job_keys["osdc.io/runner-class"]["operator"] == "DoesNotExist"

    def test_default_disk_size(self, tmp_path):
        """When disk_size is omitted from def, default 100 is used."""
        p = tmp_path / "nodisk.yaml"
        p.write_text(
            yaml.dump(
                {
                    "runner": {
                        "name": "nodisk",
                        "instance_type": "m6i.32xlarge",
                        "vcpu": 2,
                        "memory": "4Gi",
                    }
                },
                default_flow_style=False,
            )
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "s",
            "runner_name_prefix": "",
        }

        generate_runner(p, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        content = (output_dir / "nodisk.yaml").read_text()
        assert "100Gi" in content


# ============================================================================
# Real-template invariants
#
# These tests render the actual modules/arc-runners/templates/runner.yaml.tpl
# against representative runner defs (CPU, GPU, ARM, release) and assert the
# invariants that recent rounds of changes locked in:
#   - Runner pod uses priorityClassName: arc-runner and is pinned to the
#     dedicated c7i-runner pool (literal string, not the templated fleet).
#   - Runner pod has no GPU/runner-class wiring.
#   - Workflow pod uses priorityClassName: arc-workflow and selects nodes by
#     the def's node_fleet (g4dn, c7i, m8g, r7a, ...).
#   - Workflow pod carries GPU tolerations + resources iff the def asks for
#     GPUs; non-GPU runners must have no nvidia.com/gpu references on the
#     workflow side.
# ============================================================================


# Representative runner defs used to parameterize TestRealTemplate. The fleet
# values must match what derive_fleet_name() produces for the given instance
# type — these are the workflow-side selectors.
RUNNER_VARIANTS = [
    pytest.param(
        {
            "name": "cpu-runner",
            "instance_type": "c7i.12xlarge",
            "vcpu": 8,
            "memory": 16,
            "gpu": 0,
            "disk_size": 150,
            "expected_workflow_fleet": "c7i",
        },
        id="cpu",
    ),
    pytest.param(
        {
            "name": "gpu-runner",
            "instance_type": "g4dn.8xlarge",
            "vcpu": 29,
            "memory": 115,
            "gpu": 1,
            "disk_size": 150,
            "expected_workflow_fleet": "g4dn",
        },
        id="gpu",
    ),
    pytest.param(
        {
            "name": "arm-runner",
            "instance_type": "m8g.48xlarge",
            "vcpu": 16,
            "memory": 62,
            "gpu": 0,
            "disk_size": 256,
            "expected_workflow_fleet": "m8g",
        },
        id="arm",
    ),
    pytest.param(
        {
            "name": "release-runner",
            "instance_type": "r7a.48xlarge",
            "vcpu": 8,
            "memory": 64,
            "gpu": 0,
            "disk_size": 200,
            "runner_group": "release-runners",
            "runner_class": "release",
            "expected_workflow_fleet": "r7a",
        },
        id="release",
    ),
    pytest.param(
        {
            "name": "override-runner",
            "instance_type": "g5.48xlarge",
            "vcpu": 189,
            "memory": 704,
            "gpu": 8,
            "disk_size": 600,
            "node_fleet": "g5-48xlarge",
            "expected_workflow_fleet": "g5-48xlarge",
        },
        id="override",
    ),
]

GPU_VARIANT = RUNNER_VARIANTS[1]


def _render_real(real_template, tmp_path, variant):
    """Render the real template for a given variant; return (helm, configmap, workflow_pod)."""
    def_kwargs = {
        "tmp_path": tmp_path,
        "name": variant["name"],
        "instance_type": variant["instance_type"],
        "vcpu": variant["vcpu"],
        "memory": variant["memory"],
        "gpu": variant["gpu"],
        "disk_size": variant["disk_size"],
    }
    if "runner_group" in variant:
        def_kwargs["runner_group"] = variant["runner_group"]
    if "runner_class" in variant:
        def_kwargs["runner_class"] = variant["runner_class"]
    if "node_fleet" in variant:
        def_kwargs["node_fleet"] = variant["node_fleet"]
    def_file = make_def_file(**def_kwargs)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    cluster_config = {
        "github_config_url": "https://github.com/test-org",
        "github_secret_name": "gh-secret",
        "runner_name_prefix": "real-",
    }
    assert generate_runner(def_file, real_template, cluster_config, output_dir, "arc-runners") is True
    docs = list(yaml.safe_load_all((output_dir / f"{variant['name']}.yaml").read_text()))
    assert len(docs) == 2
    helm, configmap = docs
    assert configmap["kind"] == "ConfigMap"
    workflow_pod = yaml.safe_load(configmap["data"]["job-pod.yaml"])
    return helm, configmap, workflow_pod


def _listener_env_value(helm, name):
    """Return the value of a named env var on the listener container.

    Returns None when the env var is absent so callers can distinguish
    "missing" from "present but empty".
    """
    containers = helm["listenerTemplate"]["spec"]["containers"]
    for entry in containers[0].get("env", []):
        if entry.get("name") == name:
            return entry.get("value")
    return None


class TestRealTemplate:
    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_uses_arc_runner_priority_class(self, real_template, tmp_path, variant):
        """Runner pod must declare priorityClassName: arc-runner."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        assert helm["template"]["spec"]["priorityClassName"] == "arc-runner"

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_pinned_to_c7i_runner_fleet(self, real_template, tmp_path, variant):
        """Runner pod nodeSelector must use the literal c7i-runner pool, not the def's fleet."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        assert helm["template"]["spec"]["nodeSelector"]["node-fleet"] == "c7i-runner"

    def test_workflow_pod_scheduler_name_from_def(self, real_template, tmp_path):
        """Workflow pod gets schedulerName when the runner def sets scheduler_name."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
            scheduler_name="bin-pack-scheduler",
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules={"bin-pack-scheduler"},
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert workflow_pod["spec"]["schedulerName"] == "bin-pack-scheduler"
        # The capacity placeholder (ph-w-*) must use the SAME scheduler so it
        # reserves a slot the real workflow pod can actually claim.
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == "bin-pack-scheduler"

    def test_workflow_pod_scheduler_name_from_cluster_default(self, real_template, tmp_path):
        """A def without its own scheduler_name inherits arc-runners.scheduler_name."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
            "scheduler_name": "bin-pack-scheduler",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules={"bin-pack-scheduler"},
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert workflow_pod["spec"]["schedulerName"] == "bin-pack-scheduler"
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == "bin-pack-scheduler"

    def test_def_scheduler_name_overrides_cluster_default(self, real_template, tmp_path):
        """A def's own scheduler_name wins over the cluster default."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
            scheduler_name="def-sched",
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
            "scheduler_name": "cluster-sched",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules={"def-sched", "cluster-sched"},
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert workflow_pod["spec"]["schedulerName"] == "def-sched"
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == "def-sched"

    def test_workflow_pod_scheduler_name_dropped_when_module_absent_cluster_default(self, real_template, tmp_path):
        """Cluster default scheduler_name is dropped when the module is not deployed."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
            "scheduler_name": "bin-pack-scheduler",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules=set(),
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert "schedulerName" not in workflow_pod["spec"]
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == ""

    def test_workflow_pod_scheduler_name_dropped_when_module_absent_per_def(self, real_template, tmp_path):
        """Per-def scheduler_name is dropped when the module is not deployed."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
            scheduler_name="bin-pack-scheduler",
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules=set(),
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert "schedulerName" not in workflow_pod["spec"]
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == ""

    def test_workflow_pod_scheduler_name_enabled_when_module_present(self, real_template, tmp_path):
        """Cluster default scheduler_name is stamped when the module is in the cluster modules list."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
            "scheduler_name": "bin-pack-scheduler",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules={"bin-pack-scheduler", "pypi-cache"},
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert workflow_pod["spec"]["schedulerName"] == "bin-pack-scheduler"
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == "bin-pack-scheduler"

    def test_workflow_pod_per_def_scheduler_dropped_no_fallback_to_cluster_default(self, real_template, tmp_path):
        """Per-def scheduler_name wins resolution; if its module is absent it is dropped, with no fallback to the cluster default even when that one's module IS deployed."""
        def_file = make_def_file(
            tmp_path=tmp_path,
            name="packed-runner",
            instance_type="r7a.48xlarge",
            vcpu=8,
            memory=64,
            gpu=0,
            disk_size=200,
            scheduler_name="def-sched",
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "real-",
            "scheduler_name": "cluster-sched",
        }
        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                available_modules={"cluster-sched"},
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "packed-runner.yaml").read_text()))
        helm = docs[0]
        workflow_pod = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        assert "schedulerName" not in workflow_pod["spec"]
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == ""

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_workflow_pod_no_scheduler_name_by_default(self, real_template, tmp_path, variant):
        """Workflow pod must NOT have schedulerName when the runner def omits scheduler_name."""
        helm, _, workflow_pod = _render_real(real_template, tmp_path, variant)
        assert "schedulerName" not in workflow_pod["spec"]
        # Placeholder scheduler env must be present-but-empty (the fork treats
        # empty as default-scheduler), keeping it in sync with the workflow pod.
        assert _listener_env_value(helm, "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME") == ""

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_tolerates_c7i_runner_node_fleet(self, real_template, tmp_path, variant):
        """Runner pod tolerations must include node-fleet=c7i-runner."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        tolerations = helm["template"]["spec"]["tolerations"]
        node_fleet_tols = [t for t in tolerations if t.get("key") == "node-fleet"]
        assert len(node_fleet_tols) == 1, f"expected exactly one node-fleet toleration, got {node_fleet_tols!r}"
        assert node_fleet_tols[0]["value"] == "c7i-runner"

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_has_no_wait_for_node_taints_env(self, real_template, tmp_path, variant):
        """Runner pod must not set ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS or its _TIMEOUT_SECONDS variant."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        runner_container = next(c for c in helm["template"]["spec"]["containers"] if c["name"] == "runner")
        env_names = [e["name"] for e in runner_container.get("env", [])]
        forbidden = [n for n in env_names if n.startswith("ACTIONS_RUNNER_WAIT_FOR_NODE_TAINTS")]
        assert forbidden == [], f"runner pod must not carry WAIT_FOR_NODE_TAINTS env vars, got {forbidden!r}"

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_has_no_gpu_toleration(self, real_template, tmp_path, variant):
        """Runner pod must never tolerate nvidia.com/gpu — GPU scheduling is workflow-side only."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        toleration_keys = [t.get("key") for t in helm["template"]["spec"]["tolerations"]]
        assert "nvidia.com/gpu" not in toleration_keys

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_has_no_runner_class_node_selector(self, real_template, tmp_path, variant):
        """Runner pod must not select on osdc.io/runner-class — that's a workflow-pool concern."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        assert "osdc.io/runner-class" not in helm["template"]["spec"]["nodeSelector"]

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_runner_pod_has_no_affinity(self, real_template, tmp_path, variant):
        """Runner pod must not set affinity — runner-class isolation is workflow-side only."""
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        assert "affinity" not in helm["template"]["spec"]

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_workflow_pod_uses_arc_workflow_priority_class(self, real_template, tmp_path, variant):
        """Workflow pod must declare priorityClassName: arc-workflow."""
        _, _, workflow_pod = _render_real(real_template, tmp_path, variant)
        assert workflow_pod["spec"]["priorityClassName"] == "arc-workflow"

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_workflow_pod_node_fleet_matches_def(self, real_template, tmp_path, variant):
        """Workflow pod's node-fleet wiring (toleration + affinity) must use the def's fleet name."""
        _, _, workflow_pod = _render_real(real_template, tmp_path, variant)
        expected_fleet = variant["expected_workflow_fleet"]

        # Toleration side
        node_fleet_tols = [t for t in workflow_pod["spec"]["tolerations"] if t.get("key") == "node-fleet"]
        assert len(node_fleet_tols) == 1
        assert node_fleet_tols[0]["value"] == expected_fleet

        # Affinity side
        prefs = workflow_pod["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        match_exprs = prefs[0]["preference"]["matchExpressions"]
        node_fleet_exprs = [e for e in match_exprs if e["key"] == "node-fleet"]
        assert len(node_fleet_exprs) == 1
        assert node_fleet_exprs[0]["values"] == [expected_fleet]

    def test_gpu_workflow_pod_has_gpu_toleration_and_resources(self, real_template, tmp_path):
        """GPU runner's workflow pod must carry nvidia.com/gpu toleration and matching request/limit."""
        _, _, workflow_pod = _render_real(real_template, tmp_path, GPU_VARIANT.values[0])
        gpu_tols = [t for t in workflow_pod["spec"]["tolerations"] if t.get("key") == "nvidia.com/gpu"]
        assert len(gpu_tols) == 1
        container = workflow_pod["spec"]["containers"][0]
        assert container["resources"]["requests"]["nvidia.com/gpu"] == "1"
        assert container["resources"]["limits"]["nvidia.com/gpu"] == "1"

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_listener_env_has_capacity_aware_runner_node_fleet(self, real_template, tmp_path, variant):
        """Listener container must declare CAPACITY_AWARE_RUNNER_NODE_FLEET=c7i-runner.

        The fork's capacity monitor uses this to place placeholder-runner pods on the
        dedicated runner pool (cluster-wide constant, same value for every scale set).
        Without this env var, the listener fails to start when capacity-aware mode is
        enabled (Validate() errors out).
        """
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        listener = next(c for c in helm["listenerTemplate"]["spec"]["containers"] if c["name"] == "listener")
        listener_env = {e["name"]: e.get("value") for e in listener["env"]}
        assert listener_env["CAPACITY_AWARE_RUNNER_NODE_FLEET"] == "c7i-runner"

    @pytest.mark.parametrize("variant", RUNNER_VARIANTS)
    def test_listener_env_runner_node_fleet_distinct_from_workflow_node_fleet(self, real_template, tmp_path, variant):
        """CAPACITY_AWARE_RUNNER_NODE_FLEET (runner pool, constant) must be distinct from
        CAPACITY_AWARE_NODE_FLEET (per-def workflow fleet). Both must be present.
        """
        helm, _, _ = _render_real(real_template, tmp_path, variant)
        listener = next(c for c in helm["listenerTemplate"]["spec"]["containers"] if c["name"] == "listener")
        listener_env = {e["name"]: e.get("value") for e in listener["env"]}
        # Workflow fleet derives from the def's instance type
        assert listener_env["CAPACITY_AWARE_NODE_FLEET"] == variant["expected_workflow_fleet"]
        # Runner fleet is the cluster-wide constant
        assert listener_env["CAPACITY_AWARE_RUNNER_NODE_FLEET"] == "c7i-runner"
        # Sanity: the two env vars must coexist as separate entries
        assert listener_env["CAPACITY_AWARE_NODE_FLEET"] != listener_env["CAPACITY_AWARE_RUNNER_NODE_FLEET"]

    @pytest.mark.parametrize(
        "variant", [v for v in RUNNER_VARIANTS if v.values[0]["gpu"] == 0], ids=lambda v: v["name"]
    )
    def test_non_gpu_workflow_pod_has_no_nvidia_references(self, real_template, tmp_path, variant):
        """Non-GPU runner must not have any nvidia.com/gpu references on the workflow side."""
        _, configmap, workflow_pod = _render_real(real_template, tmp_path, variant)

        # Tolerations
        toleration_keys = [t.get("key") for t in workflow_pod["spec"]["tolerations"]]
        assert "nvidia.com/gpu" not in toleration_keys

        # Container resources
        container = workflow_pod["spec"]["containers"][0]
        assert "nvidia.com/gpu" not in container["resources"].get("requests", {})
        assert "nvidia.com/gpu" not in container["resources"].get("limits", {})

        # Affinity matchExpressions
        prefs = workflow_pod["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        match_expr_keys = [e["key"] for e in prefs[0]["preference"]["matchExpressions"]]
        assert "nvidia.com/gpu" not in match_expr_keys

        # Belt-and-braces: the rendered job-pod.yaml block scalar should also be free of GPU mentions
        assert "nvidia.com/gpu" not in configmap["data"]["job-pod.yaml"]


# ============================================================================
# main() integration tests
# ============================================================================


class TestMain:
    @pytest.fixture(autouse=True)
    def _runner_image_env(self, monkeypatch):
        monkeypatch.setenv(
            "RUNNER_IMAGE",
            "ghcr.io/actions/actions-runner:2.333.1@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )

    def test_no_args_exits_1(self):
        with patch.object(sys, "argv", ["generate_runners.py"]):
            assert main() == 1

    def test_unknown_cluster_exits_1(self, tmp_path, monkeypatch):
        # Write clusters.yaml
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(tmp_path / "defs"))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path / "out"))

        # Create template
        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "nonexistent"]):
            assert main() == 1

    def test_missing_github_url_exits_1(self, tmp_path, monkeypatch):
        # Config with NO defaults for arc-runners and a cluster that has no arc-runners config
        no_url_config = {
            "defaults": {},
            "clusters": {
                "bare": {
                    "cluster_name": "bare-cluster",
                    "region": "us-east-1",
                    "modules": [],
                },
            },
        }
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(no_url_config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner1", "m6i.32xlarge", 2, 4)

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path / "out"))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "bare"]):
            assert main() == 1

    def test_full_generation(self, tmp_path, monkeypatch):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner-a", "m6i.32xlarge", 2, 4)
        make_def_file(defs_dir, "runner-b", "g4dn.8xlarge", 4, 16, gpu=1)
        make_nodepool_defs(tmp_path, ["m6i.32xlarge", "g4dn.8xlarge"])

        output_dir = tmp_path / "out"

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "staging"]):
            assert main() == 0

        generated = sorted(output_dir.glob("*.yaml"))
        assert len(generated) == 2
        assert generated[0].name == "runner-a.yaml"
        assert generated[1].name == "runner-b.yaml"

    def test_runner_image_from_env(self, tmp_path, monkeypatch):
        """main() renders RUNNER_IMAGE env var into the runner template; no clusters.yaml key needed."""
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner-a", "m6i.32xlarge", 2, 4)
        make_nodepool_defs(tmp_path, ["m6i.32xlarge"])

        output_dir = tmp_path / "out"

        explicit_ref = "ghcr.io/actions/actions-runner:2.999.0@sha256:1111111111111111111111111111111111111111111111111111111111111111"
        monkeypatch.setenv("RUNNER_IMAGE", explicit_ref)
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "staging"]):
            assert main() == 0

        rendered = (output_dir / "runner-a.yaml").read_text()
        assert explicit_ref in rendered

    def test_output_dir_cleaned(self, tmp_path, monkeypatch):
        """Output dir is cleaned before generation so stale files are removed."""
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner-a", "m6i.32xlarge", 2, 4)
        make_nodepool_defs(tmp_path, ["m6i.32xlarge"])

        output_dir = tmp_path / "out"
        output_dir.mkdir()
        # Pre-existing stale file
        (output_dir / "stale-runner.yaml").write_text("old")

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "staging"]):
            assert main() == 0

        generated = list(output_dir.glob("*.yaml"))
        names = [f.name for f in generated]
        assert "stale-runner.yaml" not in names
        assert "runner-a.yaml" in names

    def test_no_def_files_exits_1(self, tmp_path, monkeypatch):
        """Lines 226-227: empty defs directory exits with error."""
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        # defs_dir is empty — no *.yaml files

        output_dir = tmp_path / "out"

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "staging"]):
            assert main() == 1

    def test_production_preserves_proactive_capacity(self, tmp_path, monkeypatch):
        """main() does NOT force proactive_capacity to zero for production clusters."""
        prod_config = {
            "defaults": {
                "arc-runners": {
                    "github_config_url": "https://github.com/prod-org",
                    "github_secret_name": "prod-secret",
                    "runner_name_prefix": "prod-",
                },
            },
            "clusters": {
                "arc-prod": {
                    "cluster_name": "production",
                    "region": "us-east-1",
                    "modules": ["nodepools", "arc-runners"],
                    "arc-runners": {
                        "github_config_url": "https://github.com/prod-org",
                        "github_secret_name": "prod-secret",
                        "runner_name_prefix": "prod-",
                    },
                },
            },
        }
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(prod_config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "warm-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "arc-prod"]):
            assert main() == 0

        docs = list(yaml.safe_load_all((output_dir / "warm-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "30"

    def test_proactive_capacity_max_clusters_yaml(self, tmp_path, monkeypatch):
        """main() honors proactive_capacity_max: 0 in clusters.yaml."""
        prod_config = {
            "defaults": {
                "arc-runners": {
                    "github_config_url": "https://github.com/prod-org",
                    "github_secret_name": "prod-secret",
                    "runner_name_prefix": "prod-",
                },
            },
            "clusters": {
                "arc-prod": {
                    "cluster_name": "production",
                    "region": "us-east-1",
                    "modules": ["nodepools", "arc-runners"],
                    "proactive_capacity_max": 0,
                    "arc-runners": {
                        "github_config_url": "https://github.com/prod-org",
                        "github_secret_name": "prod-secret",
                        "runner_name_prefix": "prod-",
                    },
                },
            },
        }
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(prod_config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "warm-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "arc-prod"]):
            assert main() == 0

        docs = list(yaml.safe_load_all((output_dir / "warm-runner.yaml").read_text()))
        listener_env = {e["name"]: e["value"] for e in docs[0]["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert listener_env["CAPACITY_AWARE_PROACTIVE_CAPACITY"] == "0"

    def test_proactive_capacity_max_invalid_clusters_yaml(self, tmp_path, monkeypatch):
        """main() exits 1 when proactive_capacity_max is not a non-negative integer."""
        invalid_config = {
            "defaults": {
                "arc-runners": {
                    "github_config_url": "https://github.com/prod-org",
                    "github_secret_name": "prod-secret",
                    "runner_name_prefix": "prod-",
                },
            },
            "clusters": {
                "arc-prod": {
                    "cluster_name": "production",
                    "region": "us-east-1",
                    "modules": ["nodepools", "arc-runners"],
                    "proactive_capacity_max": -1,
                    "arc-runners": {
                        "github_config_url": "https://github.com/prod-org",
                        "github_secret_name": "prod-secret",
                        "runner_name_prefix": "prod-",
                    },
                },
            },
        }
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(invalid_config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "warm-runner", "c7i.24xlarge", 4, 16, proactive_capacity=30)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path / "out"))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "arc-prod"]):
            assert main() == 1

    def test_pause_runners_forces_max_runners_zero(self, tmp_path, monkeypatch):
        """main() honors cluster-level pause_runners=true by forcing maxRunners: 0."""
        paused_config = {
            "defaults": {
                "arc-runners": {
                    "github_config_url": "https://github.com/prod-org",
                    "github_secret_name": "prod-secret",
                    "runner_name_prefix": "prod-",
                },
            },
            "clusters": {
                "arc-prod": {
                    "cluster_name": "production",
                    "region": "us-east-1",
                    "modules": ["nodepools", "arc-runners"],
                    "pause_runners": True,
                    "arc-runners": {
                        "github_config_url": "https://github.com/prod-org",
                        "github_secret_name": "prod-secret",
                        "runner_name_prefix": "prod-",
                    },
                },
            },
        }
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(paused_config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "capped-runner", "c7i.24xlarge", 4, 16, max_runners=8)
        make_def_file(defs_dir, "elastic-runner", "c7i.24xlarge", 4, 16)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "tpl.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        (tmp_path / "tpl.yaml").write_text(MINIMAL_TEMPLATE)

        with patch.object(sys, "argv", ["generate_runners.py", "arc-prod"]):
            assert main() == 0

        capped_docs = list(yaml.safe_load_all((output_dir / "capped-runner.yaml").read_text()))
        assert capped_docs[0]["maxRunners"] == 0

        elastic_docs = list(yaml.safe_load_all((output_dir / "elastic-runner.yaml").read_text()))
        assert elastic_docs[0]["maxRunners"] == 0

    def test_missing_template_exits_1(self, tmp_path, monkeypatch):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "nonexistent.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path / "out"))

        with patch.object(sys, "argv", ["generate_runners.py", "staging"]):
            assert main() == 1

    def test_additional_orgs_fan_out(self, tmp_path, monkeypatch, real_template):
        """End-to-end: a cluster with additional_orgs emits both the primary
        {def}.yaml and one {slug}-{def}.yaml per additional org."""
        config = {
            "defaults": {},
            "clusters": {
                "multi-org": {
                    "cluster_name": "multi-org",
                    "region": "us-east-2",
                    "modules": ["nodepools", "arc-runners"],
                    "arc-runners": {
                        "github_config_url": "https://github.com/pytorch",
                        "github_secret_name": "primary-secret",
                        "runner_name_prefix": "mt-",
                        "runner_group": "multi-org",
                        "additional_orgs": [
                            {
                                "github_config_url": "https://github.com/meta-pytorch",
                                "github_secret_name": "mp-secret",
                                "runner_group": "mp-group",
                                "resource_slug": "mp",
                                "max_runners": 12,
                            },
                        ],
                    },
                },
            },
        }
        (tmp_path / "clusters.yaml").write_text(yaml.dump(config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "l-x86ic-8-16", "c7i.24xlarge", 4, 16)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"
        tpl_file = tmp_path / "tpl.yaml"
        tpl_file.write_text(real_template)

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tpl_file))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        with patch.object(sys, "argv", ["generate_runners.py", "multi-org"]):
            assert main() == 0

        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        assert generated == ["l-x86ic-8-16.yaml", "mp-l-x86ic-8-16.yaml"]

        # Primary keeps the bare name and emits no resourceName.
        primary = list(yaml.safe_load_all((output_dir / "l-x86ic-8-16.yaml").read_text()))
        assert "resourceName" not in primary[0]
        assert primary[0]["githubConfigUrl"] == "https://github.com/pytorch"
        assert primary[0]["runnerScaleSetName"] == "mt-l-x86ic-8-16"

        # Additional org points at its own org/secret/group and shares the label.
        extra = list(yaml.safe_load_all((output_dir / "mp-l-x86ic-8-16.yaml").read_text()))
        assert extra[0]["resourceName"] == "mp-l-x86ic-8-16"
        assert extra[0]["githubConfigUrl"] == "https://github.com/meta-pytorch"
        assert extra[0]["githubConfigSecret"] == "mp-secret"
        assert extra[0]["runnerGroup"] == "mp-group"
        assert extra[0]["maxRunners"] == 12
        assert extra[0]["runnerScaleSetName"] == "mt-l-x86ic-8-16"

    def test_two_additional_orgs_fan_out(self, tmp_path, monkeypatch, real_template):
        """A cluster with TWO additional orgs emits the primary plus one file per
        org per def (3 files/def), each with a distinct resourceName/url/secret/
        group/maxRunners and the SAME shared GitHub label."""
        config = {
            "defaults": {},
            "clusters": {
                "multi-org": {
                    "cluster_name": "multi-org",
                    "region": "us-east-2",
                    "modules": ["nodepools", "arc-runners"],
                    "arc-runners": {
                        "github_config_url": "https://github.com/pytorch",
                        "github_secret_name": "primary-secret",
                        "runner_name_prefix": "mt-",
                        "runner_group": "multi-org",
                        "additional_orgs": [
                            {
                                "github_config_url": "https://github.com/meta-pytorch",
                                "github_secret_name": "mp-secret",
                                "runner_group": "mp-group",
                                "resource_slug": "mp",
                                "max_runners": 12,
                            },
                            {
                                "github_config_url": "https://github.com/executorch",
                                "github_secret_name": "et-secret",
                                "runner_group": "et-group",
                                "resource_slug": "et",
                                "max_runners": 7,
                            },
                        ],
                    },
                },
            },
        }
        (tmp_path / "clusters.yaml").write_text(yaml.dump(config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "l-x86ic-8-16", "c7i.24xlarge", 4, 16)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"
        tpl_file = tmp_path / "tpl.yaml"
        tpl_file.write_text(real_template)

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tpl_file))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        with patch.object(sys, "argv", ["generate_runners.py", "multi-org"]):
            assert main() == 0

        generated = sorted(f.name for f in output_dir.glob("*.yaml"))
        assert generated == ["et-l-x86ic-8-16.yaml", "l-x86ic-8-16.yaml", "mp-l-x86ic-8-16.yaml"]

        primary = list(yaml.safe_load_all((output_dir / "l-x86ic-8-16.yaml").read_text()))
        assert "resourceName" not in primary[0]
        assert primary[0]["githubConfigUrl"] == "https://github.com/pytorch"

        mp = list(yaml.safe_load_all((output_dir / "mp-l-x86ic-8-16.yaml").read_text()))
        assert mp[0]["resourceName"] == "mp-l-x86ic-8-16"
        assert mp[0]["githubConfigUrl"] == "https://github.com/meta-pytorch"
        assert mp[0]["githubConfigSecret"] == "mp-secret"
        assert mp[0]["runnerGroup"] == "mp-group"
        assert mp[0]["maxRunners"] == 12

        et = list(yaml.safe_load_all((output_dir / "et-l-x86ic-8-16.yaml").read_text()))
        assert et[0]["resourceName"] == "et-l-x86ic-8-16"
        assert et[0]["githubConfigUrl"] == "https://github.com/executorch"
        assert et[0]["githubConfigSecret"] == "et-secret"
        assert et[0]["runnerGroup"] == "et-group"
        assert et[0]["maxRunners"] == 7

        # The GitHub runner label ({prefix}{def}) is shared across all three.
        assert primary[0]["runnerScaleSetName"] == "mt-l-x86ic-8-16"
        assert mp[0]["runnerScaleSetName"] == "mt-l-x86ic-8-16"
        assert et[0]["runnerScaleSetName"] == "mt-l-x86ic-8-16"

    def _run_additional_orgs(self, tmp_path, monkeypatch, real_template, additional_orgs, def_name="l-x86ic-8-16"):
        """Set up a single-def cluster carrying the given additional_orgs and run
        main(); return its exit code. Used by the validation-failure tests."""
        config = {
            "defaults": {},
            "clusters": {
                "multi-org": {
                    "cluster_name": "multi-org",
                    "region": "us-east-2",
                    "modules": ["nodepools", "arc-runners"],
                    "arc-runners": {
                        "github_config_url": "https://github.com/pytorch",
                        "github_secret_name": "primary-secret",
                        "runner_name_prefix": "mt-",
                        "additional_orgs": additional_orgs,
                    },
                },
            },
        }
        (tmp_path / "clusters.yaml").write_text(yaml.dump(config, default_flow_style=False))
        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, def_name, "c7i.24xlarge", 4, 16)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])
        tpl_file = tmp_path / "tpl.yaml"
        tpl_file.write_text(real_template)
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tpl_file))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path / "out"))
        with patch.object(sys, "argv", ["generate_runners.py", "multi-org"]):
            return main()

    def test_additional_org_missing_key_fails_cleanly(self, tmp_path, monkeypatch, real_template, capsys):
        """A missing required key fails via log_error / exit 1 — not a raw KeyError."""
        rc = self._run_additional_orgs(
            tmp_path,
            monkeypatch,
            real_template,
            [{"github_config_url": "https://github.com/meta-pytorch", "resource_slug": "mp"}],  # no secret
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "github_secret_name" in captured.out + captured.err

    def test_additional_org_invalid_slug_fails_cleanly(self, tmp_path, monkeypatch, real_template, capsys):
        """A non-DNS-1123 resource_slug fails cleanly via log_error / exit 1."""
        rc = self._run_additional_orgs(tmp_path, monkeypatch, real_template, [_org_entry(resource_slug="Bad_Slug")])
        assert rc == 1
        captured = capsys.readouterr()
        assert "DNS-1123" in captured.out + captured.err

    def test_additional_org_over_length_name_fails_cleanly(self, tmp_path, monkeypatch, real_template, capsys):
        """A {slug}-{def} name over 45 chars fails at generate time, not at helm apply."""
        rc = self._run_additional_orgs(
            tmp_path,
            monkeypatch,
            real_template,
            [_org_entry(resource_slug="mp")],
            def_name="a" * 44,  # "mp-" + 44 = 47 > 45
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "chart limit" in captured.out + captured.err

    def test_additional_org_duplicate_slug_fails_no_overwrite(self, tmp_path, monkeypatch, real_template, capsys):
        """Two additional orgs with the same resource_slug fail cleanly, and no
        output is written (no silent overwrite)."""
        rc = self._run_additional_orgs(
            tmp_path,
            monkeypatch,
            real_template,
            [_org_entry(resource_slug="mp"), _org_entry(resource_slug="mp", github_secret_name="mp2-secret")],
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "duplicate" in captured.out + captured.err
        # Failure happens before the output dir is touched — nothing overwritten.
        assert not (tmp_path / "out").exists()


# ============================================================================
# Conditional block stripping (pypi-cache cluster-scope gate)
# ============================================================================


# Names of the pypi-cache env vars injected on the workflow pod when the
# pypi-cache module is enabled. When the module is disabled, NONE of these may
# appear in the rendered runner config — otherwise jobs reach for a Service
# that doesn't exist on this cluster and pip/uv install fails.
PYPI_CACHE_ENV_VAR_NAMES = [
    "PIP_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "PIP_EXTRA_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INSECURE_HOST",
    "UV_INDEX",
    "UV_INDEX_STRATEGY",
    "PYPI_CACHE_SIMPLE_URL",
    "PYPI_CACHE_WHL_URL",
]


class TestPypiCacheConditional:
    """Cluster-scoped gating of the PYPI_CACHE env-var block in the workflow pod."""

    def test_pypi_cache_block_kept_when_module_enabled(self, tmp_path, real_template):
        """With pypi-cache in the modules list, all 9 env vars render and the
        marker comments are cleanly stripped from the output."""
        def_file = make_def_file(tmp_path, "kept-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                pypi_cache_enabled=True,
            )
            is True
        )

        content = (output_dir / "kept-runner.yaml").read_text()
        # Marker comments must NOT leak into the rendered output.
        assert "# BEGIN_PYPI_CACHE" not in content
        assert "# END_PYPI_CACHE" not in content

        docs = list(yaml.safe_load_all(content))
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        env_vars = {e["name"]: e["value"] for e in container["env"]}
        for name in PYPI_CACHE_ENV_VAR_NAMES:
            assert name in env_vars, f"{name} missing from rendered env when pypi-cache enabled"

    def test_pypi_cache_block_stripped_when_module_disabled(self, tmp_path, real_template):
        """With pypi-cache absent from the modules list, NONE of the 9 env vars
        render and the marker comments are stripped."""
        def_file = make_def_file(tmp_path, "stripped-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                pypi_cache_enabled=False,
            )
            is True
        )

        content = (output_dir / "stripped-runner.yaml").read_text()
        # Marker comments must NOT leak into the rendered output.
        assert "# BEGIN_PYPI_CACHE" not in content
        assert "# END_PYPI_CACHE" not in content

        docs = list(yaml.safe_load_all(content))
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        env_vars = {e["name"]: e["value"] for e in container["env"]}
        for name in PYPI_CACHE_ENV_VAR_NAMES:
            assert name not in env_vars, f"{name} must NOT be present when pypi-cache disabled"
        # Other env vars (e.g. TORCH_CI_MAX_MEMORY) must still be intact.
        assert "TORCH_CI_MAX_MEMORY" in env_vars
        # Belt-and-braces: the rendered config must not mention the Service host either.
        assert "pypi-cache-cpu.pypi-cache.svc.cluster.local" not in content

    def test_main_strips_when_cluster_lacks_pypi_cache_module(self, tmp_path, monkeypatch, real_template):
        """End-to-end: main() reads modules from clusters.yaml and strips when pypi-cache is absent."""
        config = {
            "defaults": {
                "arc": {"runner_image_tag": "2.333.1"},
                "arc-runners": {
                    "github_config_url": "https://github.com/org",
                    "github_secret_name": "secret",
                    "runner_name_prefix": "p-",
                },
            },
            "clusters": {
                "no-cache-cluster": {
                    "cluster_name": "no-cache",
                    "region": "us-east-1",
                    "modules": ["nodepools", "arc-runners"],  # no pypi-cache
                    "arc-runners": {
                        "github_config_url": "https://github.com/org",
                        "github_secret_name": "secret",
                        "runner_name_prefix": "p-",
                    },
                },
            },
        }
        (tmp_path / "clusters.yaml").write_text(yaml.dump(config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner-x", "c7i.24xlarge", 4, 16)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"
        tpl_file = tmp_path / "tpl.yaml"
        tpl_file.write_text(real_template)

        monkeypatch.setenv("RUNNER_IMAGE", "ghcr.io/actions/actions-runner:2.333.1")
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tpl_file))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        with patch.object(sys, "argv", ["generate_runners.py", "no-cache-cluster"]):
            assert main() == 0

        content = (output_dir / "runner-x.yaml").read_text()
        for name in PYPI_CACHE_ENV_VAR_NAMES:
            assert name not in content
        assert "# BEGIN_PYPI_CACHE" not in content

    def test_main_keeps_when_cluster_has_pypi_cache_module(self, tmp_path, monkeypatch, real_template):
        """End-to-end: main() preserves the env vars when pypi-cache is in modules."""
        config = {
            "defaults": {
                "arc": {"runner_image_tag": "2.333.1"},
                "arc-runners": {
                    "github_config_url": "https://github.com/org",
                    "github_secret_name": "secret",
                    "runner_name_prefix": "p-",
                },
            },
            "clusters": {
                "with-cache-cluster": {
                    "cluster_name": "with-cache",
                    "region": "us-east-1",
                    "modules": ["nodepools", "arc-runners", "pypi-cache"],
                    "arc-runners": {
                        "github_config_url": "https://github.com/org",
                        "github_secret_name": "secret",
                        "runner_name_prefix": "p-",
                    },
                },
            },
        }
        (tmp_path / "clusters.yaml").write_text(yaml.dump(config, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner-y", "c7i.24xlarge", 4, 16)
        make_nodepool_defs(tmp_path, ["c7i.24xlarge"])

        output_dir = tmp_path / "out"
        tpl_file = tmp_path / "tpl.yaml"
        tpl_file.write_text(real_template)

        monkeypatch.setenv("RUNNER_IMAGE", "ghcr.io/actions/actions-runner:2.333.1")
        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_DEFS_DIR", str(defs_dir))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tpl_file))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(output_dir))

        with patch.object(sys, "argv", ["generate_runners.py", "with-cache-cluster"]):
            assert main() == 0

        content = (output_dir / "runner-y.yaml").read_text()
        for name in PYPI_CACHE_ENV_VAR_NAMES:
            assert name in content
        assert "# BEGIN_PYPI_CACHE" not in content
        assert "# END_PYPI_CACHE" not in content


class TestImexChannels:
    """Optional NVIDIA_IMEX_CHANNELS env var on the workflow job container."""

    def _cluster_config(self):
        return {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

    def test_imex_channels_set_injects_env(self, tmp_path, real_template):
        """imex_channels: 0 renders NVIDIA_IMEX_CHANNELS="0" on the job container.

        0 is deliberate: it is falsy, so this also proves the gate uses
        ``is not None`` rather than truthiness (a truthiness check would drop 0).
        """
        def_file = make_def_file(
            tmp_path, "imex-runner", "p6-b200.48xlarge", 22, 225, gpu=1, disk_size=600, imex_channels=0
        )
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, real_template, self._cluster_config(), output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "imex-runner.yaml").read_text()))
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        env_vars = {e["name"]: e["value"] for e in cm_data["spec"]["containers"][0]["env"]}
        assert env_vars["NVIDIA_IMEX_CHANNELS"] == "0"

    def test_imex_channels_unset_omits_env(self, tmp_path, real_template):
        """Without imex_channels, NVIDIA_IMEX_CHANNELS is absent from the job container."""
        def_file = make_def_file(tmp_path, "plain-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert generate_runner(def_file, real_template, self._cluster_config(), output_dir, "arc-runners") is True

        docs = list(yaml.safe_load_all((output_dir / "plain-runner.yaml").read_text()))
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        env_names = [e["name"] for e in cm_data["spec"]["containers"][0]["env"]]
        assert "NVIDIA_IMEX_CHANNELS" not in env_names


def _fresh_multiplier_env_value(output_dir, runner_name):
    docs = list(yaml.safe_load_all((output_dir / f"{runner_name}.yaml").read_text()))
    helm = docs[0]
    env = {e["name"]: e.get("value") for e in helm["listenerTemplate"]["spec"]["containers"][0]["env"]}
    return env.get("CAPACITY_AWARE_FRESH_MULTIPLIER")


class TestMultipliers:
    """fresh_multiplier lookup order: per-def → cluster_cfg[module_name].capacity_aware_fresh_multiplier → 1.0."""

    def _base_cluster_config(self):
        return {
            "github_config_url": "https://github.com/test-org",
            "github_secret_name": "gh-secret",
            "runner_name_prefix": "",
        }

    def test_defaults_to_one_when_neither_set(self, tmp_path):
        def_file = make_def_file(tmp_path, "r", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        assert generate_runner(def_file, MINIMAL_TEMPLATE, self._base_cluster_config(), output_dir, "arc-runners")
        assert _fresh_multiplier_env_value(output_dir, "r") == "1.0"

    def test_per_def_value_wins_over_module_block(self, tmp_path):
        def_file = make_def_file(tmp_path, "r", "c7i.24xlarge", 4, 16, fresh_multiplier=0.5)
        cluster_cfg = {"arc-runners": {"capacity_aware_fresh_multiplier": 3.0}}
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        assert generate_runner(
            def_file,
            MINIMAL_TEMPLATE,
            self._base_cluster_config(),
            output_dir,
            "arc-runners",
            cluster_cfg=cluster_cfg,
        )
        assert _fresh_multiplier_env_value(output_dir, "r") == "0.5"

    def test_module_block_fallback_applies_when_def_unset(self, tmp_path):
        def_file = make_def_file(tmp_path, "r", "c7i.24xlarge", 4, 16)
        cluster_cfg = {"arc-runners": {"capacity_aware_fresh_multiplier": 0.8}}
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        assert generate_runner(
            def_file,
            MINIMAL_TEMPLATE,
            self._base_cluster_config(),
            output_dir,
            "arc-runners",
            cluster_cfg=cluster_cfg,
        )
        assert _fresh_multiplier_env_value(output_dir, "r") == "0.8"

    def test_module_block_lookup_is_per_module_not_shared(self, tmp_path):
        """h100 reads its own block, not the sibling arc-runners block."""
        def_file = make_def_file(tmp_path, "r", "p5.48xlarge", 22, 225, gpu=1)
        cluster_cfg = {
            "arc-runners": {"capacity_aware_fresh_multiplier": 0.8},
            "arc-runners-h100": {"capacity_aware_fresh_multiplier": 1.0},
        }
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        assert generate_runner(
            def_file,
            MINIMAL_TEMPLATE,
            self._base_cluster_config(),
            output_dir,
            "arc-runners-h100",
            cluster_cfg=cluster_cfg,
        )
        assert _fresh_multiplier_env_value(output_dir, "r") == "1.0"

    def test_int_value_coerced_to_float_string(self, tmp_path):
        def_file = make_def_file(tmp_path, "r", "c7i.24xlarge", 4, 16, fresh_multiplier=2)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        assert generate_runner(def_file, MINIMAL_TEMPLATE, self._base_cluster_config(), output_dir, "arc-runners")
        assert _fresh_multiplier_env_value(output_dir, "r") == "2.0"

    def test_bad_value_exits_one(self, tmp_path):
        def_file = make_def_file(tmp_path, "bad", "c7i.24xlarge", 4, 16, fresh_multiplier="abc")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        with pytest.raises(SystemExit) as exc:
            generate_runner(def_file, MINIMAL_TEMPLATE, self._base_cluster_config(), output_dir, "arc-runners")
        assert exc.value.code == 1


class TestComputeClusterSharding:
    """Unit tests for the per-(module, runner_name_prefix) sharding helper."""

    def _yaml(self, clusters):
        return {"clusters": clusters}

    def test_two_peer_clusters_same_module_same_prefix(self):
        yml = self._yaml(
            {
                "a-prod-aws-ue1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "mt-"}},
                "a-prod-aws-ue2": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "mt-"}},
            }
        )
        assert compute_cluster_sharding(yml, "a-prod-aws-ue1", "arc-runners", "mt-") == (0, 2)
        assert compute_cluster_sharding(yml, "a-prod-aws-ue2", "arc-runners", "mt-") == (1, 2)

    def test_different_prefixes_are_separate_shards(self):
        yml = self._yaml(
            {
                "meta-prod-aws-ue1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "mt-"}},
                "lf-prod-aws-ue1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "lf-"}},
            }
        )
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-") == (0, 1)
        assert compute_cluster_sharding(yml, "lf-prod-aws-ue1", "arc-runners", "lf-") == (0, 1)

    def test_different_modules_are_separate_shards(self):
        yml = self._yaml(
            {
                "meta-prod-aws-ue1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "mt-"}},
                "meta-prod-aws-ue2": {
                    "modules": ["arc-runners", "arc-runners-b200"],
                    "arc-runners": {"runner_name_prefix": "mt-"},
                },
            }
        )
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-") == (0, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-") == (1, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners-b200", "mt-") == (0, 1)

    def test_three_peer_staging_clusters(self):
        yml = self._yaml(
            {
                "meta-staging-aws-uw1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "c-mt-"}},
                "meta-staging-aws-ue1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "c-mt-"}},
                "meta-staging-aws-ue2": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "c-mt-"}},
            }
        )
        assert compute_cluster_sharding(yml, "meta-staging-aws-ue1", "arc-runners", "c-mt-") == (0, 3)
        assert compute_cluster_sharding(yml, "meta-staging-aws-ue2", "arc-runners", "c-mt-") == (1, 3)
        assert compute_cluster_sharding(yml, "meta-staging-aws-uw1", "arc-runners", "c-mt-") == (2, 3)

    def test_cluster_not_in_peer_set_returns_safe_fallback(self):
        yml = self._yaml(
            {
                "other": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "mt-"}},
            }
        )
        assert compute_cluster_sharding(yml, "missing", "arc-runners", "mt-") == (0, 1)

    def test_empty_clusters_yaml(self):
        assert compute_cluster_sharding({}, "any", "arc-runners", "mt-") == (0, 1)
        assert compute_cluster_sharding({"clusters": {}}, "any", "arc-runners", "mt-") == (0, 1)

    def test_empty_prefix_matches_clusters_with_no_prefix(self):
        yml = self._yaml(
            {
                "x": {"modules": ["arc-runners"], "arc-runners": {}},
                "y": {"modules": ["arc-runners"]},
            }
        )
        assert compute_cluster_sharding(yml, "x", "arc-runners", "") == (0, 2)
        assert compute_cluster_sharding(yml, "y", "arc-runners", "") == (1, 2)

    def test_opt_variant_shards_with_base_module(self):
        yml = self._yaml(
            {
                "meta-prod-aws-ue1": {
                    "modules": ["arc-runners-opt"],
                    "arc-runners": {"runner_name_prefix": "mt-"},
                },
                "meta-prod-aws-ue2": {
                    "modules": ["arc-runners"],
                    "arc-runners": {"runner_name_prefix": "mt-"},
                },
            }
        )
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners-opt", "mt-") == (0, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-") == (1, 2)

    def test_non_opt_suffix_is_separate_shard(self):
        yml = self._yaml(
            {
                "meta-prod-aws-ue1": {"modules": ["arc-runners"], "arc-runners": {"runner_name_prefix": "mt-"}},
                "meta-prod-aws-uw2": {"modules": ["arc-runners-h100"], "arc-runners": {"runner_name_prefix": "mt-"}},
            }
        )
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-") == (0, 1)
        assert compute_cluster_sharding(yml, "meta-prod-aws-uw2", "arc-runners-h100", "mt-") == (0, 1)


# ============================================================================
# org_key_of
# ============================================================================


class TestOrgKeyOf:
    def test_org_scoped_url(self):
        assert org_key_of("https://github.com/pytorch") == "pytorch"

    def test_repo_scoped_url_returns_owner(self):
        assert org_key_of("https://github.com/pytorch/pytorch-canary") == "pytorch"

    def test_lowercased(self):
        assert org_key_of("https://github.com/Meta-PyTorch") == "meta-pytorch"

    def test_trailing_slash(self):
        assert org_key_of("https://github.com/pytorch/") == "pytorch"

    def test_empty_string(self):
        assert org_key_of("") == ""

    def test_none(self):
        assert org_key_of(None) == ""

    def test_non_github_url(self):
        assert org_key_of("https://example.com/pytorch") == ""

    def test_bare_github_no_owner(self):
        assert org_key_of("https://github.com/") == ""


# ============================================================================
# _cluster_serves_org
# ============================================================================


class TestClusterServesOrg:
    def test_primary_org_matches(self):
        cfg = {"arc-runners": {"github_config_url": "https://github.com/pytorch"}}
        assert _cluster_serves_org(cfg, "pytorch") is True

    def test_additional_org_matches(self):
        cfg = {
            "arc-runners": {
                "github_config_url": "https://github.com/pytorch",
                "additional_orgs": [{"github_config_url": "https://github.com/meta-pytorch"}],
            }
        }
        assert _cluster_serves_org(cfg, "meta-pytorch") is True

    def test_unserved_org_returns_false(self):
        cfg = {
            "arc-runners": {
                "github_config_url": "https://github.com/pytorch",
                "additional_orgs": [{"github_config_url": "https://github.com/meta-pytorch"}],
            }
        }
        assert _cluster_serves_org(cfg, "other-org") is False

    def test_missing_arc_runners_block(self):
        assert _cluster_serves_org({}, "pytorch") is False

    def test_no_additional_orgs(self):
        cfg = {"arc-runners": {"github_config_url": "https://github.com/pytorch"}}
        assert _cluster_serves_org(cfg, "meta-pytorch") is False


# ============================================================================
# compute_cluster_sharding — org-aware peer scoping
# ============================================================================


class TestComputeClusterShardingOrgAware:
    """org_key scoping: None reproduces the org-agnostic peer set; a real org
    restricts peers to clusters that serve it."""

    def _yaml(self, clusters):
        return {"clusters": clusters}

    def test_org_key_none_matches_org_agnostic_result(self):
        """Passing org_key='pytorch' when every peer serves pytorch is a no-op
        versus the org-agnostic (org_key=None) result — this is the byte-identical
        guarantee at the sharding level."""
        yml = self._yaml(
            {
                "meta-prod-aws-ue1": {
                    "modules": ["arc-runners"],
                    "arc-runners": {"runner_name_prefix": "mt-", "github_config_url": "https://github.com/pytorch"},
                },
                "meta-prod-aws-ue2": {
                    "modules": ["arc-runners"],
                    "arc-runners": {"runner_name_prefix": "mt-", "github_config_url": "https://github.com/pytorch"},
                },
            }
        )
        for cid in ("meta-prod-aws-ue1", "meta-prod-aws-ue2"):
            agnostic = compute_cluster_sharding(yml, cid, "arc-runners", "mt-")
            scoped = compute_cluster_sharding(yml, cid, "arc-runners", "mt-", org_key="pytorch")
            assert scoped == agnostic

    def test_additional_org_forms_its_own_group_pytorch_unchanged(self):
        """A cluster with an additional org: the pytorch shard is unchanged (both
        peers serve pytorch), while the additional org is its own group of one
        (only the cluster carrying it serves that org)."""
        yml = self._yaml(
            {
                "meta-prod-aws-ue1": {
                    "modules": ["arc-runners"],
                    "arc-runners": {"runner_name_prefix": "mt-", "github_config_url": "https://github.com/pytorch"},
                },
                "meta-prod-aws-ue2": {
                    "modules": ["arc-runners"],
                    "arc-runners": {
                        "runner_name_prefix": "mt-",
                        "github_config_url": "https://github.com/pytorch",
                        "additional_orgs": [{"github_config_url": "https://github.com/meta-pytorch"}],
                    },
                },
            }
        )
        # pytorch shard unchanged from the org-agnostic split.
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-", org_key="pytorch") == (0, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-", org_key="pytorch") == (1, 2)
        # meta-pytorch is served only by ue2 -> a group of one.
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-", org_key="meta-pytorch") == (
            0,
            1,
        )
        # ue1 does not serve meta-pytorch -> safe single-cluster fallback.
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-", org_key="meta-pytorch") == (
            0,
            1,
        )


# ============================================================================
# generate_runner — additional-org fan-out (org_override)
# ============================================================================


def _org_override(**overrides):
    """A complete additional_orgs entry (with precomputed _idx/_count)."""
    org = {
        "github_config_url": "https://github.com/meta-pytorch",
        "github_secret_name": "mp-secret",
        "runner_group": "mp-group",
        "resource_slug": "mp",
        "max_runners": 12,
        "_idx": 1,
        "_count": 3,
    }
    org.update(overrides)
    return org


class TestGenerateRunnerOrgOverride:
    def _cluster_config(self):
        return {
            "github_config_url": "https://github.com/pytorch",
            "github_secret_name": "primary-secret",
            "runner_name_prefix": "mt-",
            "runner_group": "meta-prod-aws-ue2",
            "cluster_id": "meta-prod-aws-ue2",
        }

    def test_org_override_full_contract(self, tmp_path, real_template):
        """One additional-org scale set: org-unique file/name/secret/url/group/
        shard/maxRunners, SHARED GitHub label, resourceName stamped."""
        def_file = make_def_file(tmp_path, "l-x86ic-8-16", "c7i.24xlarge", 8, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert (
            generate_runner(
                def_file,
                real_template,
                self._cluster_config(),
                output_dir,
                "arc-runners",
                org_override=_org_override(),
            )
            is True
        )

        # File is keyed by resource_id ({slug}-{def}), not the bare def name.
        out_file = output_dir / "mp-l-x86ic-8-16.yaml"
        assert out_file.exists()
        assert not (output_dir / "l-x86ic-8-16.yaml").exists()

        docs = list(yaml.safe_load_all(out_file.read_text()))
        helm, cm = docs
        # Shared GitHub label = {prefix}{def} (drives runner registration).
        assert helm["runnerScaleSetName"] == "mt-l-x86ic-8-16"
        # resourceName makes the chart name all K8s objects after the resource_id.
        assert helm["resourceName"] == "mp-l-x86ic-8-16"
        # url / secret / group all come from the override.
        assert helm["githubConfigUrl"] == "https://github.com/meta-pytorch"
        assert helm["githubConfigSecret"] == "mp-secret"
        assert helm["runnerGroup"] == "mp-group"
        # maxRunners from the override.
        assert helm["maxRunners"] == 12
        # Shard index/count from the override's precomputed _idx/_count.
        env = {e["name"]: e.get("value") for e in helm["listenerTemplate"]["spec"]["containers"][0]["env"]}
        assert env["CAPACITY_AWARE_CLUSTER_INDEX"] == "1"
        assert env["CAPACITY_AWARE_CLUSTER_COUNT"] == "3"
        # Hook ConfigMap is named after normalize(resource_id) and the runner pod
        # references that same ConfigMap.
        assert cm["metadata"]["name"] == "arc-runner-hook-mp-l-x86ic-8-16"
        assert helm["template"]["spec"]["volumes"][-1]["configMap"]["name"] == "arc-runner-hook-mp-l-x86ic-8-16"

    def test_org_override_configmap_name_normalized(self, tmp_path, real_template):
        """resource_id with dots/underscores is normalized for the ConfigMap name."""
        def_file = make_def_file(tmp_path, "runner.with_dots", "m6i.32xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert (
            generate_runner(
                def_file,
                real_template,
                self._cluster_config(),
                output_dir,
                "arc-runners",
                org_override=_org_override(),
            )
            is True
        )
        out_file = output_dir / "mp-runner.with_dots.yaml"
        assert out_file.exists()
        docs = list(yaml.safe_load_all(out_file.read_text()))
        assert docs[1]["metadata"]["name"] == "arc-runner-hook-mp-runner-with-dots"

    def test_org_override_max_runners_falls_back_to_def(self, tmp_path, real_template):
        """An override without max_runners resolves the cap from the def instead."""
        def_file = make_def_file(tmp_path, "capped", "c7i.24xlarge", 4, 16, max_runners=8)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        org = _org_override()
        del org["max_runners"]

        assert (
            generate_runner(
                def_file,
                real_template,
                self._cluster_config(),
                output_dir,
                "arc-runners",
                org_override=org,
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "mp-capped.yaml").read_text()))
        assert docs[0]["maxRunners"] == 8

    def test_org_override_repo_scoped_url_forces_default_group(self, tmp_path, real_template):
        """The repo-scope guard still applies to an additional org: a repo-scoped
        override URL forces runnerGroup back to 'default'."""
        def_file = make_def_file(tmp_path, "repo-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert (
            generate_runner(
                def_file,
                real_template,
                self._cluster_config(),
                output_dir,
                "arc-runners",
                org_override=_org_override(github_config_url="https://github.com/meta-pytorch/some-repo"),
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "mp-repo-runner.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "default"

    def test_org_override_none_output_unchanged(self, tmp_path, real_template):
        """org_override=None keeps the primary contract: file is {def}.yaml, no
        resourceName line is emitted, name uses the bare def."""
        def_file = make_def_file(tmp_path, "primary-runner", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        assert (
            generate_runner(
                def_file,
                real_template,
                self._cluster_config(),
                output_dir,
                "arc-runners",
            )
            is True
        )
        out_file = output_dir / "primary-runner.yaml"
        assert out_file.exists()
        content = out_file.read_text()
        assert "resourceName:" not in content
        docs = list(yaml.safe_load_all(content))
        assert "resourceName" not in docs[0]
        assert docs[0]["runnerScaleSetName"] == "mt-primary-runner"
        assert docs[1]["metadata"]["name"] == "arc-runner-hook-primary-runner"

    def test_org_override_max_runners_zeroed_by_pause_runners(self, tmp_path, real_template):
        """pause_runners=true forces the additional org's maxRunners to 0 even
        when the override supplies its own max_runners."""
        def_file = make_def_file(tmp_path, "paused", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = self._cluster_config()
        cluster_config["pause_runners"] = True

        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                org_override=_org_override(),
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "mp-paused.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0

    def test_org_override_max_runners_zeroed_by_region_exclusion(self, tmp_path, real_template):
        """A region-excluded instance_type forces the additional org's maxRunners
        to 0 even when the override supplies its own max_runners."""
        def_file = make_def_file(tmp_path, "excluded", "c7i.24xlarge", 4, 16)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = self._cluster_config()
        cluster_config["region"] = "us-west-1"
        cluster_config["excluded_instance_types"] = {"c7i.24xlarge"}

        assert (
            generate_runner(
                def_file,
                real_template,
                cluster_config,
                output_dir,
                "arc-runners",
                org_override=_org_override(),
            )
            is True
        )
        docs = list(yaml.safe_load_all((output_dir / "mp-excluded.yaml").read_text()))
        assert docs[0]["maxRunners"] == 0


# ============================================================================
# _load_runner_names
# ============================================================================


class TestLoadRunnerNames:
    """_load_runner_names returns runner.name only for defs that emit output."""

    def test_includes_valid_defs_and_skips_incomplete(self, tmp_path):
        good = make_def_file(tmp_path, "good-runner", "c7i.24xlarge", 4, 16)
        # A def missing name or instance_type writes nothing in generate_runner,
        # so it must be excluded from the preflight name set too.
        no_type = tmp_path / "no-type.yaml"
        no_type.write_text(yaml.dump({"runner": {"name": "no-type"}}, default_flow_style=False))
        no_name = tmp_path / "no-name.yaml"
        no_name.write_text(yaml.dump({"runner": {"instance_type": "c7i.24xlarge"}}, default_flow_style=False))

        assert _load_runner_names(sorted([good, no_type, no_name])) == ["good-runner"]

    def test_empty_list(self):
        assert _load_runner_names([]) == []


# ============================================================================
# _additional_org_errors
# ============================================================================


class TestAdditionalOrgErrors:
    """Pure validation of a cluster's additional_orgs entries."""

    def test_valid_two_orgs_no_errors(self):
        orgs = [_org_entry(resource_slug="mp"), _org_entry(resource_slug="mp2", github_secret_name="mp2-secret")]
        assert _additional_org_errors("c1", orgs, ["l-x86ic-8-16"]) == []

    def test_missing_required_keys_each_reported(self):
        errors = _additional_org_errors("c1", [{}], ["r"])
        assert len(errors) == 3
        joined = "\n".join(errors)
        for key in ("github_config_url", "github_secret_name", "resource_slug"):
            assert key in joined
        assert "additional_orgs[0]" in joined

    def test_blank_required_value_reported(self):
        errors = _additional_org_errors("c1", [_org_entry(github_secret_name="   ")], ["r"])
        assert any("github_secret_name" in e for e in errors)

    def test_none_entry_treated_as_empty(self):
        # A null list item must fail cleanly (all keys missing), never raise.
        assert len(_additional_org_errors("c1", [None], ["r"])) == 3

    def test_invalid_slug_rejected(self):
        errors = _additional_org_errors("c1", [_org_entry(resource_slug="Bad_Slug")], ["r"])
        assert len(errors) == 1
        assert "DNS-1123" in errors[0]
        assert "Bad_Slug" in errors[0]

    @pytest.mark.parametrize("slug", ["-lead", "trail-", "UP", "a/b", "a_b", "a.b"])
    def test_invalid_slug_shapes(self, slug):
        assert any("DNS-1123" in e for e in _additional_org_errors("c1", [_org_entry(resource_slug=slug)], ["r"]))

    def test_duplicate_slug_rejected(self):
        orgs = [_org_entry(resource_slug="mp"), _org_entry(resource_slug="mp", github_secret_name="s2")]
        errors = _additional_org_errors("c1", orgs, ["r"])
        assert len(errors) == 1
        assert "duplicate" in errors[0]
        assert "additional_orgs[1]" in errors[0]

    def test_over_length_name_rejected(self):
        long_name = "a" * 44  # "mp-" + 44 = 47 > 45
        errors = _additional_org_errors("c1", [_org_entry(resource_slug="mp")], [long_name])
        assert len(errors) == 1
        assert "47" in errors[0]
        assert "45" in errors[0]

    def test_name_at_limit_allowed(self):
        name = "a" * 42  # "mp-" + 42 = 45, exactly the limit
        assert _additional_org_errors("c1", [_org_entry(resource_slug="mp")], [name]) == []

    def test_collision_with_primary_rejected(self):
        # Primary emits "mp-a" for a def named "mp-a"; org slug "mp" also emits
        # "mp-a" for the def named "a" -> collision, no silent overwrite.
        errors = _additional_org_errors("c1", [_org_entry(resource_slug="mp")], ["a", "mp-a"])
        assert any("collides" in e and "mp-a" in e for e in errors)

    def test_collision_between_two_orgs_rejected(self):
        # slug "a" + def "b-c" -> "a-b-c"; slug "a-b" + def "c" -> "a-b-c".
        orgs = [_org_entry(resource_slug="a"), _org_entry(resource_slug="a-b", github_secret_name="s2")]
        errors = _additional_org_errors("c1", orgs, ["b-c", "c"])
        assert any("collides" in e and "a-b-c" in e for e in errors)


# ============================================================================
# compute_cluster_sharding — org-aware, real multi-cluster fan-out
# ============================================================================


class TestComputeClusterShardingMultiClusterOrg:
    """Two clusters both serve the same additional org, so that org shards across
    both (count=2), while their shared primary org sharding is unchanged."""

    def test_same_additional_org_on_two_clusters(self):
        arc = {
            "runner_name_prefix": "mt-",
            "github_config_url": "https://github.com/pytorch",
            "additional_orgs": [{"github_config_url": "https://github.com/meta-pytorch"}],
        }
        yml = {
            "clusters": {
                "meta-prod-aws-ue1": {"modules": ["arc-runners"], "arc-runners": dict(arc)},
                "meta-prod-aws-ue2": {"modules": ["arc-runners"], "arc-runners": dict(arc)},
            }
        }
        # meta-pytorch is served by BOTH clusters -> a real 2-cluster shard group.
        mp = "meta-pytorch"
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-", org_key=mp) == (0, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-", org_key=mp) == (1, 2)
        # The shared primary (pytorch) shard is unchanged by the additional org.
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-", org_key="pytorch") == (0, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-", org_key="pytorch") == (1, 2)
        # Byte-identical: the org-agnostic split matches the primary split.
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue1", "arc-runners", "mt-") == (0, 2)
        assert compute_cluster_sharding(yml, "meta-prod-aws-ue2", "arc-runners", "mt-") == (1, 2)
