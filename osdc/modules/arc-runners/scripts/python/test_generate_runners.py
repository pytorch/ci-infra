"""Tests for generate_runners.py."""

import sys
import textwrap
from unittest.mock import patch

import pytest
import yaml
from generate_runners import (
    generate_runner,
    get_cluster_config,
    load_clusters_yaml,
    main,
    normalize_name,
    parse_memory_bytes,
    resolve_value,
)

# ============================================================================
# Fixtures
# ============================================================================

MINIMAL_TEMPLATE = textwrap.dedent("""\
    githubConfigUrl: "{{GITHUB_CONFIG_URL}}"
    githubConfigSecret: "{{GITHUB_SECRET_NAME}}"
    runnerScaleSetName: "{{RUNNER_NAME_PREFIX}}{{RUNNER_NAME}}"
    runnerGroup: "{{RUNNER_GROUP}}"
    template:
      spec:
        containers:
          - name: runner
            image: {{RUNNER_IMAGE}}
        nodeSelector:
          instance-type: "{{INSTANCE_TYPE}}"
    {{RUNNER_CLASS_NODE_SELECTOR}}{{RUNNER_CLASS_AFFINITY}}
        tolerations:
          - key: instance-type
            operator: Equal
            value: "{{INSTANCE_TYPE}}"
            effect: NoSchedule{{GPU_TOLERATIONS}}
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
                      - key: instance-type
                        operator: In
                        values:
                          - "{{INSTANCE_TYPE}}"
                      - key: workload-type
                        operator: In
                        values:
                          - github-runner{{GPU_NODE_SELECTOR_AFFINITY}}
          tolerations:
            - key: instance-type
              operator: Equal
              value: "{{INSTANCE_TYPE}}"
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
""")

FAKE_CLUSTERS_YAML = {
    "defaults": {
        "arc": {
            "runner_image_tag": "2.333.1",
        },
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
            "modules": ["arc-runners"],
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
    tmp_path, name, instance_type, vcpu, memory, gpu=0, disk_size=100, runner_group=None, runner_class=None
):
    """Write a runner def YAML and return the path."""
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
    content = {"runner": runner}
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.dump(content, default_flow_style=False))
    return p


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
# generate_runner
# ============================================================================


class TestGenerateRunner:
    def test_non_gpu_runner(self, tmp_path):
        def_file = make_def_file(tmp_path, "cpu-runner", "m5.xlarge", 4, 16)
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

        # Helm values doc — runner pod still uses hard nodeSelector
        helm = docs[0]
        assert helm["githubConfigUrl"] == "https://github.com/test-org"
        assert helm["githubConfigSecret"] == "gh-secret"
        assert helm["runnerScaleSetName"] == "staging-cpu-runner"
        assert helm["template"]["spec"]["nodeSelector"]["instance-type"] == "m5.xlarge"

        # ConfigMap doc
        cm = docs[1]
        assert cm["kind"] == "ConfigMap"
        assert cm["metadata"]["name"] == "arc-runner-hook-cpu-runner"
        assert cm["metadata"]["labels"]["osdc.io/module"] == "arc-runners"

        # Job pod uses soft affinity, not nodeSelector
        cm_data = yaml.safe_load(cm["data"]["job-pod.yaml"])
        assert "nodeSelector" not in cm_data["spec"]
        prefs = cm_data["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        assert len(prefs) == 1
        assert prefs[0]["weight"] == 50
        match_exprs = prefs[0]["preference"]["matchExpressions"]
        keys = [e["key"] for e in match_exprs]
        assert "instance-type" in keys
        assert "workload-type" in keys
        assert "nvidia.com/gpu" not in keys  # CPU runner has no GPU affinity

    def test_gpu_runner(self, tmp_path):
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

        # GPU tolerations should be present
        assert "nvidia.com/gpu" in content

        docs = list(yaml.safe_load_all(content))
        helm = docs[0]
        tolerations = helm["template"]["spec"]["tolerations"]
        taint_keys = [t["key"] for t in tolerations]
        assert "nvidia.com/gpu" in taint_keys

        # ConfigMap job-pod data should have GPU resources and GPU affinity
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        container = cm_data["spec"]["containers"][0]
        assert "nvidia.com/gpu" in container["resources"]["requests"]
        assert "nvidia.com/gpu" in container["resources"]["limits"]

        # Job pod affinity should include nvidia.com/gpu matchExpression
        assert "nodeSelector" not in cm_data["spec"]
        prefs = cm_data["spec"]["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        assert len(prefs) == 1
        match_exprs = prefs[0]["preference"]["matchExpressions"]
        keys = [e["key"] for e in match_exprs]
        assert "instance-type" in keys
        assert "workload-type" in keys
        assert "nvidia.com/gpu" in keys

    def test_no_placeholders_remaining(self, tmp_path):
        def_file = make_def_file(tmp_path, "test-runner", "c5.xlarge", 2, 4)
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "https://github.com/org",
            "github_secret_name": "secret",
            "runner_name_prefix": "pre-",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        content = (output_dir / "test-runner.yaml").read_text()
        assert "{{" not in content
        assert "}}" not in content

    def test_normalized_name_in_output(self, tmp_path):
        def_file = make_def_file(tmp_path, "runner.with_dots", "m5.xlarge", 4, 16)
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
        p.write_text(yaml.dump({"runner": {"instance_type": "m5.xlarge"}}, default_flow_style=False))
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

    def test_resource_values_match_def(self, tmp_path):
        def_file = make_def_file(tmp_path, "res-test", "c6i.12xlarge", 48, 96, disk_size=200)
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
        def_file = make_def_file(tmp_path, "mem-test", "c6i.12xlarge", 48, 96, disk_size=200)
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
        def_file = make_def_file(tmp_path, "cache-test", "m5.xlarge", 4, 16)
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
        def_file = make_def_file(tmp_path, "grp-test", "m5.xlarge", 4, 16)
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
        """Runner group can be overridden in the def."""
        def_file = make_def_file(tmp_path, "rel-grp", "m5.xlarge", 4, 16, runner_group="release-runners")
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        cluster_config = {
            "github_config_url": "url",
            "github_secret_name": "secret",
            "runner_name_prefix": "",
        }

        generate_runner(def_file, MINIMAL_TEMPLATE, cluster_config, output_dir, "arc-runners")
        docs = list(yaml.safe_load_all((output_dir / "rel-grp.yaml").read_text()))
        assert docs[0]["runnerGroup"] == "release-runners"

    def test_runner_class_release(self, tmp_path):
        """Release runners get nodeSelector and required job affinity."""
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

        # Runner pod should have runner-class nodeSelector
        assert helm["template"]["spec"]["nodeSelector"]["osdc.io/runner-class"] == "release"

        # Runner pod should NOT have affinity (release runners skip anti-affinity)
        assert "affinity" not in helm["template"]["spec"]

        # Job pod should have required affinity for runner-class=release
        cm_data = yaml.safe_load(docs[1]["data"]["job-pod.yaml"])
        required = cm_data["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        match_exprs = required["nodeSelectorTerms"][0]["matchExpressions"]
        keys = {e["key"]: e for e in match_exprs}
        assert "osdc.io/runner-class" in keys
        assert keys["osdc.io/runner-class"]["operator"] == "In"
        assert keys["osdc.io/runner-class"]["values"] == ["release"]

    def test_regular_runner_anti_affinity(self, tmp_path):
        """Regular runners get anti-affinity to avoid release nodes."""
        def_file = make_def_file(tmp_path, "reg-runner", "m5.xlarge", 4, 16)
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

        # No runner-class in nodeSelector
        assert "osdc.io/runner-class" not in helm["template"]["spec"]["nodeSelector"]

        # Runner pod SHOULD have anti-affinity (DoesNotExist)
        affinity = helm["template"]["spec"]["affinity"]
        required = affinity["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
        match_exprs = required["nodeSelectorTerms"][0]["matchExpressions"]
        keys = {e["key"]: e for e in match_exprs}
        assert "osdc.io/runner-class" in keys
        assert keys["osdc.io/runner-class"]["operator"] == "DoesNotExist"

        # Job pod should also have DoesNotExist required affinity
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
                        "instance_type": "m5.xlarge",
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
# main() integration tests
# ============================================================================


class TestMain:
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
        make_def_file(defs_dir, "runner1", "m5.xlarge", 2, 4)

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
        make_def_file(defs_dir, "runner-a", "m5.xlarge", 2, 4)
        make_def_file(defs_dir, "runner-b", "g4dn.xlarge", 4, 16, gpu=1)

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

    def test_output_dir_cleaned(self, tmp_path, monkeypatch):
        """Output dir is cleaned before generation so stale files are removed."""
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        defs_dir = tmp_path / "defs"
        defs_dir.mkdir()
        make_def_file(defs_dir, "runner-a", "m5.xlarge", 2, 4)

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

    def test_missing_template_exits_1(self, tmp_path, monkeypatch):
        p = tmp_path / "clusters.yaml"
        p.write_text(yaml.dump(FAKE_CLUSTERS_YAML, default_flow_style=False))

        monkeypatch.setenv("OSDC_ROOT", str(tmp_path))
        monkeypatch.setenv("ARC_RUNNERS_TEMPLATE", str(tmp_path / "nonexistent.yaml"))
        monkeypatch.setenv("ARC_RUNNERS_OUTPUT_DIR", str(tmp_path / "out"))

        with patch.object(sys, "argv", ["generate_runners.py", "staging"]):
            assert main() == 1
