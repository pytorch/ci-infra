"""Tests for daemonset_overhead module."""

import textwrap
from pathlib import Path

import pytest
import yaml
from daemonset_overhead import (
    EKS_ADDON_DAEMONSETS,
    HELM_DAEMONSETS,
    _discover_from_yaml,
    _extract_container_resources,
    _is_gpu_only,
    discover_daemonsets,
    parse_cpu_millicores,
    parse_memory_mib,
)

# ---------------------------------------------------------------------------
# parse_cpu_millicores
# ---------------------------------------------------------------------------


class TestParseCpuMillicores:
    def test_millicores_string(self):
        assert parse_cpu_millicores("100m") == 100

    def test_whole_cores_string(self):
        assert parse_cpu_millicores("2") == 2000

    def test_fractional_cores(self):
        assert parse_cpu_millicores("0.5") == 500

    def test_integer_input(self):
        assert parse_cpu_millicores(1) == 1000

    def test_float_input(self):
        assert parse_cpu_millicores(0.25) == 250

    def test_ten_millicores(self):
        assert parse_cpu_millicores("10m") == 10


# ---------------------------------------------------------------------------
# parse_memory_mib
# ---------------------------------------------------------------------------


class TestParseMemoryMib:
    def test_mebibytes(self):
        assert parse_memory_mib("256Mi") == 256

    def test_gibibytes(self):
        assert parse_memory_mib("4Gi") == 4096

    def test_kibibytes(self):
        assert parse_memory_mib("1024Ki") == 1

    def test_plain_bytes(self):
        assert parse_memory_mib("134217728") == 128  # 128 MiB in bytes

    def test_integer_input(self):
        assert parse_memory_mib(134217728) == 128

    def test_fractional_gi(self):
        assert parse_memory_mib("1.5Gi") == 1536


# ---------------------------------------------------------------------------
# _is_gpu_only
# ---------------------------------------------------------------------------


class TestIsGpuOnly:
    def test_node_selector_nvidia(self):
        pod_spec = {"nodeSelector": {"nvidia.com/gpu": "true"}}
        assert _is_gpu_only(pod_spec) is True

    def test_node_selector_gpu_present(self):
        pod_spec = {"nodeSelector": {"nvidia.com/gpu.present": "true"}}
        assert _is_gpu_only(pod_spec) is True

    def test_affinity_nvidia(self):
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "nvidia.com/gpu.present",
                                        "operator": "In",
                                        "values": ["true"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _is_gpu_only(pod_spec) is True

    def test_workload_type_affinity_not_gpu(self):
        """workload-type: github-runner is NOT gpu-only."""
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "workload-type",
                                        "operator": "In",
                                        "values": ["github-runner"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _is_gpu_only(pod_spec) is False

    def test_no_selector_no_affinity(self):
        assert _is_gpu_only({}) is False

    def test_empty_node_selector(self):
        assert _is_gpu_only({"nodeSelector": {}}) is False


# ---------------------------------------------------------------------------
# _extract_container_resources
# ---------------------------------------------------------------------------


class TestExtractContainerResources:
    def test_single_container(self):
        containers = [{"name": "app", "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}}}]
        assert _extract_container_resources(containers) == (100, 256)

    def test_multiple_containers(self):
        containers = [
            {"name": "a", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}},
            {"name": "b", "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}}},
        ]
        assert _extract_container_resources(containers) == (150, 192)

    def test_no_resources_block(self):
        """Containers without resources contribute 0."""
        containers = [{"name": "sidecar"}]
        assert _extract_container_resources(containers) == (0, 0)

    def test_mixed_resources(self):
        """One container with resources, one without."""
        containers = [
            {"name": "main", "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}}},
            {"name": "sidecar"},
        ]
        assert _extract_container_resources(containers) == (200, 512)

    def test_empty_list(self):
        assert _extract_container_resources([]) == (0, 0)

    def test_limits_only_no_requests(self):
        """Only limits, no requests — contributes 0."""
        containers = [{"name": "app", "resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}]
        assert _extract_container_resources(containers) == (0, 0)


# ---------------------------------------------------------------------------
# _discover_from_yaml
# ---------------------------------------------------------------------------


class TestDiscoverFromYaml:
    def test_single_doc(self, tmp_path):
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "test-ds"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                },
                            }
                        ]
                    }
                }
            },
        }
        (tmp_path / "ds.yaml").write_text(yaml.dump(manifest))

        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].name == "test-ds"
        assert results[0].cpu_millicores == 50
        assert results[0].memory_mib == 64
        assert results[0].gpu_only is False

    def test_multi_doc_file(self, tmp_path):
        """Multi-document YAML (ConfigMap + DaemonSet)."""
        content = textwrap.dedent("""\
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: my-config
            data:
              key: value
            ---
            apiVersion: apps/v1
            kind: DaemonSet
            metadata:
              name: multi-doc-ds
            spec:
              template:
                spec:
                  containers:
                    - name: main
                      resources:
                        requests:
                          cpu: 10m
                          memory: 32Mi
        """)
        (tmp_path / "multi.yaml").write_text(content)

        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].name == "multi-doc-ds"
        assert results[0].cpu_millicores == 10
        assert results[0].memory_mib == 32

    def test_gpu_only_node_selector(self, tmp_path):
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "gpu-ds"},
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {"nvidia.com/gpu": "true"},
                        "containers": [{"name": "gpu"}],
                    }
                }
            },
        }
        (tmp_path / "gpu.yaml").write_text(yaml.dump(manifest))

        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].gpu_only is True

    def test_non_daemonset_ignored(self, tmp_path):
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "deploy"},
            "spec": {"template": {"spec": {"containers": [{"name": "app"}]}}},
        }
        (tmp_path / "deploy.yaml").write_text(yaml.dump(manifest))

        results = _discover_from_yaml([tmp_path])
        assert len(results) == 0

    def test_nonexistent_dir(self):
        results = _discover_from_yaml([Path("/nonexistent/path")])
        assert results == []

    def test_recursive_discovery(self, tmp_path):
        """Discovers DaemonSets in subdirectories."""
        subdir = tmp_path / "sub" / "dir"
        subdir.mkdir(parents=True)
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "nested-ds"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {"requests": {"cpu": "25m", "memory": "16Mi"}},
                            }
                        ]
                    }
                }
            },
        }
        (subdir / "nested.yaml").write_text(yaml.dump(manifest))

        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].name == "nested-ds"


# ---------------------------------------------------------------------------
# discover_daemonsets (integration)
# ---------------------------------------------------------------------------


class TestDiscoverDaemonsets:
    def test_includes_helm_by_default(self, tmp_path):
        # Create minimal upstream structure
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()

        results = discover_daemonsets(tmp_path)
        helm_names = {ds.name for ds in HELM_DAEMONSETS}
        found_names = {ds.name for ds in results}
        assert helm_names.issubset(found_names)

    def test_includes_eks_addons_by_default(self, tmp_path):
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()

        results = discover_daemonsets(tmp_path)
        eks_names = {ds.name for ds in EKS_ADDON_DAEMONSETS}
        found_names = {ds.name for ds in results}
        assert eks_names.issubset(found_names)

    def test_exclude_eks_addons(self, tmp_path):
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()

        results = discover_daemonsets(tmp_path, include_eks_addons=False)
        eks_names = {ds.name for ds in EKS_ADDON_DAEMONSETS}
        found_names = {ds.name for ds in results}
        assert not eks_names.intersection(found_names)

    def test_exclude_helm(self, tmp_path):
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()

        results = discover_daemonsets(tmp_path, include_helm=False)
        helm_names = {ds.name for ds in HELM_DAEMONSETS}
        found_names = {ds.name for ds in results}
        assert not helm_names.intersection(found_names)

    def test_deduplication_consumer_overrides(self, tmp_path):
        """Consumer module overrides upstream DaemonSet with same name."""
        # Upstream DaemonSet
        upstream = tmp_path / "upstream"
        base_k8s = upstream / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        upstream_modules = upstream / "modules" / "monitoring" / "kubernetes"
        upstream_modules.mkdir(parents=True)

        ds_manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "my-ds"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
                            }
                        ]
                    }
                }
            },
        }
        (upstream_modules / "ds.yaml").write_text(yaml.dump(ds_manifest))

        # Consumer override with different resources
        consumer = tmp_path / "consumer"
        consumer_modules = consumer / "modules" / "monitoring" / "kubernetes"
        consumer_modules.mkdir(parents=True)

        ds_override = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "my-ds"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "app",
                                "resources": {"requests": {"cpu": "200m", "memory": "256Mi"}},
                            }
                        ]
                    }
                }
            },
        }
        (consumer_modules / "ds.yaml").write_text(yaml.dump(ds_override))

        results = discover_daemonsets(
            upstream,
            consumer_root=consumer,
            include_eks_addons=False,
            include_helm=False,
        )
        my_ds = [ds for ds in results if ds.name == "my-ds"]
        assert len(my_ds) == 1
        # Consumer values win (last-writer-wins)
        assert my_ds[0].cpu_millicores == 200
        assert my_ds[0].memory_mib == 256


# ---------------------------------------------------------------------------
# Smoke test against real manifests
# ---------------------------------------------------------------------------


class TestRealManifests:
    """Smoke test using the actual project manifests (if available)."""

    @pytest.fixture
    def upstream_dir(self):
        """Resolve the upstream osdc/ directory."""
        script_dir = Path(__file__).resolve().parent
        upstream = script_dir.parent.parent  # scripts/python -> osdc/
        if not (upstream / "base" / "kubernetes").exists():
            pytest.skip("Upstream manifests not available")
        return upstream

    def test_discovers_known_daemonsets(self, upstream_dir):
        results = discover_daemonsets(upstream_dir)
        names = {ds.name for ds in results}

        # These should always be discovered from raw YAML
        expected_yaml = {
            "git-cache-warmer",
            "node-performance-tuning",
            "registry-mirror-config",
            "nvidia-device-plugin-daemonset",
            "runner-hooks-warmer",
            "dcgm-exporter",
        }
        assert expected_yaml.issubset(names), f"Missing: {expected_yaml - names}"

        # Helm constants
        assert "node-exporter" in names
        assert "alloy-logging" in names

        # EKS addon constants
        assert "kube-proxy" in names
        assert "vpc-cni" in names
        assert "ebs-csi-node" in names

    def test_gpu_only_detection(self, upstream_dir):
        results = discover_daemonsets(upstream_dir)
        by_name = {ds.name: ds for ds in results}

        # GPU-only
        assert by_name["nvidia-device-plugin-daemonset"].gpu_only is True
        assert by_name["dcgm-exporter"].gpu_only is True

        # Not GPU-only
        assert by_name["git-cache-warmer"].gpu_only is False
        assert by_name["node-performance-tuning"].gpu_only is False
        assert by_name["registry-mirror-config"].gpu_only is False
        assert by_name["runner-hooks-warmer"].gpu_only is False

    def test_resource_values_match_manifests(self, upstream_dir):
        """Verify parsed values match what's in the actual manifests."""
        results = discover_daemonsets(upstream_dir)
        by_name = {ds.name: ds for ds in results}

        # git-cache-warmer: 100m CPU, 256Mi
        assert by_name["git-cache-warmer"].cpu_millicores == 100
        assert by_name["git-cache-warmer"].memory_mib == 256

        # node-performance-tuning: 10m CPU, 32Mi (sleep container only)
        assert by_name["node-performance-tuning"].cpu_millicores == 10
        assert by_name["node-performance-tuning"].memory_mib == 32

        # registry-mirror-config: 10m CPU, 32Mi (sleep container only)
        assert by_name["registry-mirror-config"].cpu_millicores == 10
        assert by_name["registry-mirror-config"].memory_mib == 32

        # nvidia-device-plugin: no resource requests
        assert by_name["nvidia-device-plugin-daemonset"].cpu_millicores == 0
        assert by_name["nvidia-device-plugin-daemonset"].memory_mib == 0

        # dcgm-exporter: 100m CPU, 128Mi
        assert by_name["dcgm-exporter"].cpu_millicores == 100
        assert by_name["dcgm-exporter"].memory_mib == 128

        # hooks-warmer: 10m CPU, 32Mi
        assert by_name["runner-hooks-warmer"].cpu_millicores == 10
        assert by_name["runner-hooks-warmer"].memory_mib == 32
