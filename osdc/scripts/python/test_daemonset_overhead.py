"""Tests for daemonset_overhead module."""

import textwrap
from pathlib import Path

import pytest
import yaml
from analyze_node_utilization import compute_daemonset_overhead
from daemonset_overhead import (
    EKS_ADDON_DAEMONSETS,
    HELM_DAEMONSETS,
    DaemonSetOverhead,
    _discover_from_yaml,
    _extract_container_resources,
    _extract_fleet_selector,
    _is_gpu_only,
    discover_daemonsets,
    main,
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

        # dcgm-exporter: 100m CPU, 256Mi
        assert by_name["dcgm-exporter"].cpu_millicores == 100
        assert by_name["dcgm-exporter"].memory_mib == 256

        # hooks-warmer: 10m CPU, 32Mi
        assert by_name["runner-hooks-warmer"].cpu_millicores == 10
        assert by_name["runner-hooks-warmer"].memory_mib == 32


# ---------------------------------------------------------------------------
# _discover_from_yaml - YAML error handling + non-dict docs
# ---------------------------------------------------------------------------


class TestDiscoverYamlEdgeCases:
    def test_invalid_yaml_skipped(self, tmp_path):
        """Files with invalid YAML are silently skipped (lines 149-150)."""
        (tmp_path / "bad.yaml").write_text(": : : invalid: [yaml")
        results = _discover_from_yaml([tmp_path])
        assert results == []

    def test_non_dict_doc_skipped(self, tmp_path):
        """Non-dict documents (e.g., bare string) are skipped (line 154)."""
        content = "---\njust a string\n---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d\n"
        (tmp_path / "mixed.yaml").write_text(content)
        results = _discover_from_yaml([tmp_path])
        # The string doc is skipped, the Deployment doc is not a DaemonSet
        assert results == []

    def test_null_doc_skipped(self, tmp_path):
        """A YAML file that yields None docs is handled (line 154)."""
        content = "---\n---\napiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: after-null\nspec:\n  template:\n    spec:\n      containers:\n        - name: app\n          resources:\n            requests:\n              cpu: 10m\n              memory: 16Mi\n"
        (tmp_path / "null_doc.yaml").write_text(content)
        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].name == "after-null"

    def test_os_error_skipped(self, tmp_path):
        """Files that raise OSError are skipped (lines 149-150)."""
        bad_file = tmp_path / "unreadable.yaml"
        bad_file.write_text("kind: DaemonSet")
        bad_file.chmod(0o000)
        try:
            results = _discover_from_yaml([tmp_path])
            # Should not crash; the file is skipped
            assert isinstance(results, list)
        finally:
            bad_file.chmod(0o644)


# ---------------------------------------------------------------------------
# main (CLI entry point, lines 229-275)
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_default_upstream_resolution(self, capsys):
        """main() with no args resolves upstream from __file__ location."""
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "Discovered" in output
        assert "DaemonSets" in output
        # Should show totals
        assert "Total (all nodes)" in output
        assert "Total (GPU nodes)" in output

    def test_explicit_upstream_dir(self, capsys, tmp_path):
        """main() with --upstream-dir pointing to minimal structure."""
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()
        result = main(["--upstream-dir", str(tmp_path)])
        assert result == 0
        output = capsys.readouterr().out
        assert "Discovered" in output

    def test_no_eks_addons_flag(self, capsys, tmp_path):
        """main() with --no-eks-addons excludes EKS addon DaemonSets."""
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()
        result = main(["--upstream-dir", str(tmp_path), "--no-eks-addons"])
        assert result == 0
        output = capsys.readouterr().out
        # EKS addon names should NOT appear
        for ds in EKS_ADDON_DAEMONSETS:
            assert ds.name not in output

    def test_no_helm_flag(self, capsys, tmp_path):
        """main() with --no-helm excludes Helm-deployed DaemonSets."""
        base_k8s = tmp_path / "base" / "kubernetes"
        base_k8s.mkdir(parents=True)
        modules = tmp_path / "modules"
        modules.mkdir()
        result = main(["--upstream-dir", str(tmp_path), "--no-helm"])
        assert result == 0
        output = capsys.readouterr().out
        for ds in HELM_DAEMONSETS:
            assert ds.name not in output

    def test_gpu_only_tagged_in_output(self, capsys):
        """GPU-only DaemonSets are tagged with [GPU-only]."""
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "[GPU-only]" in output

    def test_shows_source_paths(self, capsys):
        """Each DaemonSet shows its source path."""
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "source:" in output


# ---------------------------------------------------------------------------
# _extract_fleet_selector
# ---------------------------------------------------------------------------


class TestExtractFleetSelector:
    def test_node_selector_node_fleet(self):
        """node-fleet via nodeSelector returns the value."""
        pod_spec = {"nodeSelector": {"node-fleet": "c7i-runner"}}
        assert _extract_fleet_selector(pod_spec) == "c7i-runner"

    def test_node_selector_other_keys_only(self):
        """nodeSelector without node-fleet returns None."""
        pod_spec = {"nodeSelector": {"workload-type": "github-runner"}}
        assert _extract_fleet_selector(pod_spec) is None

    def test_node_affinity_in_single_value(self):
        """nodeAffinity matchExpression with In + single value returns that value."""
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-fleet",
                                        "operator": "In",
                                        "values": ["g4dn"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _extract_fleet_selector(pod_spec) == "g4dn"

    def test_node_affinity_in_multiple_values(self):
        """nodeAffinity matchExpression with In + multiple values returns None."""
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-fleet",
                                        "operator": "In",
                                        "values": ["c7i-runner", "g4dn"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _extract_fleet_selector(pod_spec) is None

    def test_node_affinity_not_in_operator(self):
        """nodeAffinity matchExpression with operator != In returns None."""
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-fleet",
                                        "operator": "NotIn",
                                        "values": ["c7i-runner"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _extract_fleet_selector(pod_spec) is None

    def test_node_affinity_exists_operator(self):
        """nodeAffinity matchExpression with operator=Exists returns None."""
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-fleet",
                                        "operator": "Exists",
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _extract_fleet_selector(pod_spec) is None

    def test_node_affinity_in_zero_values(self):
        """nodeAffinity matchExpression with In + empty values returns None."""
        pod_spec = {
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-fleet",
                                        "operator": "In",
                                        "values": [],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }
        assert _extract_fleet_selector(pod_spec) is None

    def test_node_affinity_other_key_only(self):
        """nodeAffinity matchExpression with non-node-fleet key returns None."""
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
        assert _extract_fleet_selector(pod_spec) is None

    def test_no_selector_no_affinity(self):
        """Empty pod spec returns None."""
        assert _extract_fleet_selector({}) is None

    def test_empty_node_selector(self):
        """Empty nodeSelector dict returns None."""
        assert _extract_fleet_selector({"nodeSelector": {}}) is None

    def test_node_selector_takes_precedence_over_affinity(self):
        """When both nodeSelector and nodeAffinity have node-fleet, nodeSelector wins."""
        pod_spec = {
            "nodeSelector": {"node-fleet": "c7i-runner"},
            "affinity": {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "node-fleet",
                                        "operator": "In",
                                        "values": ["different"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
        }
        assert _extract_fleet_selector(pod_spec) == "c7i-runner"


# ---------------------------------------------------------------------------
# fleet_selector parsed via _discover_from_yaml
# ---------------------------------------------------------------------------


class TestDiscoverFromYamlFleetSelector:
    def test_node_selector_fleet(self, tmp_path):
        """fleet_selector parsed from spec.template.spec.nodeSelector["node-fleet"]."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "hooks-warmer"},
            "spec": {
                "template": {
                    "spec": {
                        "nodeSelector": {"node-fleet": "c7i-runner"},
                        "containers": [{"name": "main"}],
                    }
                }
            },
        }
        (tmp_path / "ds.yaml").write_text(yaml.dump(manifest))
        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].fleet_selector == "c7i-runner"

    def test_node_affinity_fleet_single_value(self, tmp_path):
        """fleet_selector parsed from nodeAffinity matchExpression with In + single value."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "affinity-pinned"},
            "spec": {
                "template": {
                    "spec": {
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchExpressions": [
                                                {
                                                    "key": "node-fleet",
                                                    "operator": "In",
                                                    "values": ["g4dn"],
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                        "containers": [{"name": "main"}],
                    }
                }
            },
        }
        (tmp_path / "ds.yaml").write_text(yaml.dump(manifest))
        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].fleet_selector == "g4dn"

    def test_node_affinity_multiple_values_is_none(self, tmp_path):
        """fleet_selector is None when matchExpression has multiple values (runs everywhere)."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "multi-fleet"},
            "spec": {
                "template": {
                    "spec": {
                        "affinity": {
                            "nodeAffinity": {
                                "requiredDuringSchedulingIgnoredDuringExecution": {
                                    "nodeSelectorTerms": [
                                        {
                                            "matchExpressions": [
                                                {
                                                    "key": "node-fleet",
                                                    "operator": "In",
                                                    "values": ["c7i-runner", "g4dn"],
                                                }
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                        "containers": [{"name": "main"}],
                    }
                }
            },
        }
        (tmp_path / "ds.yaml").write_text(yaml.dump(manifest))
        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].fleet_selector is None

    def test_no_fleet_selector(self, tmp_path):
        """fleet_selector is None when no node-fleet anywhere."""
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": {"name": "unpinned"},
            "spec": {
                "template": {"spec": {"containers": [{"name": "main"}]}},
            },
        }
        (tmp_path / "ds.yaml").write_text(yaml.dump(manifest))
        results = _discover_from_yaml([tmp_path])
        assert len(results) == 1
        assert results[0].fleet_selector is None


# ---------------------------------------------------------------------------
# fleet_selector for HELM and EKS_ADDON constants
# ---------------------------------------------------------------------------


class TestConstantsFleetSelector:
    def test_helm_constants_have_none_fleet_selector(self):
        """All HELM_DAEMONSETS run on every node — fleet_selector must be None."""
        for ds in HELM_DAEMONSETS:
            assert ds.fleet_selector is None, f"{ds.name} should have fleet_selector=None"

    def test_eks_addon_constants_have_none_fleet_selector(self):
        """All EKS_ADDON_DAEMONSETS run on every node — fleet_selector must be None."""
        for ds in EKS_ADDON_DAEMONSETS:
            assert ds.fleet_selector is None, f"{ds.name} should have fleet_selector=None"


# ---------------------------------------------------------------------------
# DaemonSetOverhead dataclass field default
# ---------------------------------------------------------------------------


class TestDaemonSetOverheadDefaultFleetSelector:
    def test_default_fleet_selector_is_none(self):
        """Constructing without fleet_selector sets it to None (backward-compatible)."""
        ds = DaemonSetOverhead(
            name="legacy",
            cpu_millicores=10,
            memory_mib=32,
            gpu_only=False,
            source="test",
        )
        assert ds.fleet_selector is None

    def test_explicit_fleet_selector(self):
        """Passing fleet_selector keyword stores it."""
        ds = DaemonSetOverhead(
            name="pinned",
            cpu_millicores=10,
            memory_mib=32,
            gpu_only=False,
            source="test",
            fleet_selector="c7i-runner",
        )
        assert ds.fleet_selector == "c7i-runner"


# ---------------------------------------------------------------------------
# compute_daemonset_overhead — fleet-aware filtering
# ---------------------------------------------------------------------------


# Mixed fleet of DaemonSets used by the fleet-aware tests below.
FLEET_DS = [
    DaemonSetOverhead("kube-proxy", 50, 80, False, "test", fleet_selector=None),
    DaemonSetOverhead("gpu-plugin", 100, 256, True, "test", fleet_selector=None),
    DaemonSetOverhead("hooks-warmer", 10, 32, False, "test", fleet_selector="c7i-runner"),
    DaemonSetOverhead("g4dn-only-tool", 25, 64, False, "test", fleet_selector="g4dn"),
]


class TestComputeDaemonsetOverheadFleetAware:
    def test_legacy_no_fleet_includes_all(self):
        """fleet_name=None (legacy) includes ALL DaemonSets (matching old behavior)."""
        cpu, mem = compute_daemonset_overhead(FLEET_DS, is_gpu=False)
        # kube-proxy (50/80) + hooks-warmer (10/32) + g4dn-only-tool (25/64).
        # gpu-plugin excluded (is_gpu=False).
        assert cpu == 50 + 10 + 25
        assert mem == 80 + 32 + 64

    def test_legacy_no_fleet_includes_all_gpu(self):
        """fleet_name=None (legacy) on GPU node includes ALL DaemonSets."""
        cpu, mem = compute_daemonset_overhead(FLEET_DS, is_gpu=True)
        # All four.
        assert cpu == 50 + 100 + 10 + 25
        assert mem == 80 + 256 + 32 + 64

    def test_c7i_runner_pool_excludes_other_fleets(self):
        """fleet_name='c7i-runner' includes None-pinned + c7i-runner-pinned only."""
        cpu, mem = compute_daemonset_overhead(FLEET_DS, is_gpu=False, fleet_name="c7i-runner")
        # kube-proxy (None) + hooks-warmer (c7i-runner). g4dn-only-tool excluded.
        # gpu-plugin excluded (is_gpu=False).
        assert cpu == 50 + 10
        assert mem == 80 + 32

    def test_g4dn_pool_with_gpu_includes_gpu_ds(self):
        """fleet_name='g4dn' on GPU node includes None + g4dn + GPU DaemonSets."""
        cpu, mem = compute_daemonset_overhead(FLEET_DS, is_gpu=True, fleet_name="g4dn")
        # kube-proxy (None) + gpu-plugin (None, gpu) + g4dn-only-tool (g4dn).
        # hooks-warmer excluded (pinned to c7i-runner).
        assert cpu == 50 + 100 + 25
        assert mem == 80 + 256 + 64

    def test_workflow_pool_excludes_runner_only_ds(self):
        """A pool name not matching any pinned DaemonSet excludes all pinned ones."""
        cpu, mem = compute_daemonset_overhead(FLEET_DS, is_gpu=False, fleet_name="workflow")
        # Only kube-proxy (None) — both hooks-warmer and g4dn-only-tool excluded.
        assert cpu == 50
        assert mem == 80

    def test_unknown_fleet_includes_unpinned_only(self):
        """Pool name with no pinned DS still returns unpinned DaemonSets."""
        cpu, mem = compute_daemonset_overhead([], is_gpu=False, fleet_name="anything")
        assert cpu == 0
        assert mem == 0

    def test_gpu_only_filtered_when_not_gpu_even_with_fleet(self):
        """gpu_only filtering still applies regardless of fleet."""
        cpu, mem = compute_daemonset_overhead(FLEET_DS, is_gpu=False, fleet_name="g4dn")
        # kube-proxy (None) + g4dn-only-tool (g4dn). gpu-plugin excluded (is_gpu=False).
        assert cpu == 50 + 25
        assert mem == 80 + 64

    def test_keyword_only_signature(self):
        """is_gpu and fleet_name are keyword-only — positional call must fail."""
        with pytest.raises(TypeError):
            # is_gpu is kw-only after the * marker.
            compute_daemonset_overhead(FLEET_DS, True)  # type: ignore[misc]
