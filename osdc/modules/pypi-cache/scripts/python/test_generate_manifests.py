#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest>=7.0", "pyyaml>=6.0"]
# ///
"""Unit tests for the generate_manifests module."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import yaml
from generate_manifests import (
    DEFAULTS,
    compute_nginx_cache_size,
    compute_pod_resources,
    cuda_slug,
    generate_deployments,
    generate_ec2nodeclass,
    generate_nodepool,
    generate_nodepools,
    generate_pvc,
    generate_services,
    generate_storageclass,
    get_slugs,
    load_config,
    log_info,
    main,
)

# ---------------------------------------------------------------------------
# Template directory — use the REAL templates from the kubernetes/ directory
# ---------------------------------------------------------------------------

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "kubernetes"


def _write_clusters_yaml(tmp_path: Path, content: str) -> Path:
    """Write a clusters.yaml file and return its path."""
    p = tmp_path / "clusters.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def _default_config() -> dict:
    """Return a copy of DEFAULTS (the baseline config)."""
    import copy

    return copy.deepcopy(DEFAULTS)


# ============================================================================
# cuda_slug
# ============================================================================


class TestCudaSlug:
    def test_12_1(self):
        assert cuda_slug("12.1") == "cu121"

    def test_12_4(self):
        assert cuda_slug("12.4") == "cu124"

    def test_11_8(self):
        assert cuda_slug("11.8") == "cu118"


# ============================================================================
# get_slugs
# ============================================================================


class TestGetSlugs:
    def test_default_config(self):
        config = _default_config()
        slugs = get_slugs(config)
        assert slugs == ["cpu"]

    def test_custom_cuda_versions(self):
        config = _default_config()
        config["cuda_versions"] = ["11.8", "12.6"]
        slugs = get_slugs(config)
        assert slugs == ["cpu", "cu118", "cu126"]

    def test_always_starts_with_cpu(self):
        config = _default_config()
        config["cuda_versions"] = ["12.1"]
        slugs = get_slugs(config)
        assert slugs[0] == "cpu"

    def test_empty_cuda_versions(self):
        config = _default_config()
        config["cuda_versions"] = []
        slugs = get_slugs(config)
        assert slugs == ["cpu"]


# ============================================================================
# load_config
# ============================================================================


class TestLoadConfig:
    def test_all_defaults_no_pypi_cache_section(self, tmp_path: Path):
        """Cluster with no pypi_cache section gets hardcoded DEFAULTS."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                cluster_name: test
            """,
        )
        config = load_config(cy, "test-cluster")
        assert config["namespace"] == "pypi-cache"
        assert config["replicas"] == 2
        assert config["image"] == "pypiserver/pypiserver:v2.4.1"
        # cuda_versions and python_versions are no longer in DEFAULTS;
        # they must be set explicitly in clusters.yaml
        assert "cuda_versions" not in config
        assert "python_versions" not in config

    def test_python_versions_override(self, tmp_path: Path):
        """Cluster-level python_versions override takes effect."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  python_versions:
                    - "3.12"
                    - "3.13"
            """,
        )
        config = load_config(cy, "test-cluster")
        assert config["python_versions"] == ["3.12", "3.13"]
        # cuda_versions is no longer in DEFAULTS; not present unless set explicitly
        assert "cuda_versions" not in config

    def test_python_versions_preserved_when_other_keys_overridden(self, tmp_path: Path):
        """Overriding unrelated keys does not drop python_versions when set in YAML."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  replicas: 5
                  python_versions:
                    - "3.12"
                    - "3.13"
            """,
        )
        config = load_config(cy, "test-cluster")
        assert config["replicas"] == 5
        assert config["python_versions"] == ["3.12", "3.13"]

    def test_cluster_overrides(self, tmp_path: Path):
        """Cluster-level pypi_cache overrides take effect."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  replicas: 5
                  namespace: custom-ns
            """,
        )
        config = load_config(cy, "test-cluster")
        assert config["replicas"] == 5
        assert config["namespace"] == "custom-ns"
        # Non-overridden defaults preserved
        assert config["image"] == "pypiserver/pypiserver:v2.4.1"

    def test_yaml_defaults_section(self, tmp_path: Path):
        """YAML-level defaults.pypi_cache applies when cluster has no override."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            defaults:
              pypi_cache:
                replicas: 3
                storage_request: "500Gi"
            clusters:
              test-cluster:
                region: us-west-2
            """,
        )
        config = load_config(cy, "test-cluster")
        assert config["replicas"] == 3
        assert config["storage_request"] == "500Gi"

    def test_merge_precedence_cluster_over_yaml_defaults(self, tmp_path: Path):
        """Cluster overrides beat yaml defaults beat hardcoded DEFAULTS."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            defaults:
              pypi_cache:
                replicas: 3
                namespace: yaml-default-ns
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  replicas: 10
            """,
        )
        config = load_config(cy, "test-cluster")
        # Cluster override wins over yaml default
        assert config["replicas"] == 10
        # YAML default wins over hardcoded (no cluster override)
        assert config["namespace"] == "yaml-default-ns"
        # Hardcoded default for fields not in yaml defaults or cluster
        assert config["image"] == "pypiserver/pypiserver:v2.4.1"

    def test_deep_merge_server_config(self, tmp_path: Path):
        """Nested server dict is deep-merged, not replaced."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  server:
                    cpu: "1"
            """,
        )
        config = load_config(cy, "test-cluster")
        # Overridden field
        assert config["server"]["cpu"] == "1"
        # Non-overridden field preserved from DEFAULTS
        assert config["server"]["memory"] == "768Mi"

    def test_deep_merge_nginx_config(self, tmp_path: Path):
        """Nested nginx dict is deep-merged, not replaced."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  nginx:
                    cpu: 4
            """,
        )
        config = load_config(cy, "test-cluster")
        # Overridden field
        assert config["nginx"]["cpu"] == 4
        # Non-overridden fields preserved from DEFAULTS
        assert config["nginx"]["memory_gi"] == 2
        assert config["nginx"]["cache_size"] == "30Gi"

    def test_unknown_cluster_raises_system_exit(self, tmp_path: Path):
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              real-cluster:
                region: us-west-2
            """,
        )
        import pytest

        with pytest.raises(SystemExit):
            load_config(cy, "nonexistent-cluster")

    def test_defaults_structure(self):
        """DEFAULTS has the expected keys for the nginx sidecar architecture."""
        config = _default_config()
        assert config["instance_type"] == "r5d.12xlarge"
        assert config["internal_port"] == 8081
        assert config["server_port"] == 8080
        assert config["workers"] == 4
        assert config["nginx_image"] == "docker.io/nginxinc/nginx-unprivileged:1.27-alpine"
        assert config["nginx"]["cpu"] == 8
        assert config["nginx"]["memory_gi"] == 2
        assert config["nginx"]["cache_size"] == "30Gi"
        assert config["server"]["cpu"] == "500m"
        assert config["server"]["memory"] == "768Mi"


# ============================================================================
# generate_storageclass
# ============================================================================


class TestGenerateStorageclass:
    def test_efs_filesystem_id_substituted(self, tmp_path: Path):
        config = _default_config()
        result = generate_storageclass(config, TEMPLATE_DIR / "storageclass.yaml.tpl", "fs-abc123")
        assert "fs-abc123" in result
        assert "__EFS_FILESYSTEM_ID__" not in result

    def test_valid_yaml(self, tmp_path: Path):
        config = _default_config()
        result = generate_storageclass(config, TEMPLATE_DIR / "storageclass.yaml.tpl", "fs-test")
        parsed = yaml.safe_load(result)
        assert parsed["kind"] == "StorageClass"
        assert parsed["metadata"]["name"] == "efs-pypi-cache"

    def test_no_remaining_placeholders(self):
        config = _default_config()
        result = generate_storageclass(config, TEMPLATE_DIR / "storageclass.yaml.tpl", "fs-test")
        assert "__" not in result


# ============================================================================
# generate_pvc
# ============================================================================


class TestGeneratePvc:
    def test_namespace_substituted(self):
        config = _default_config()
        result = generate_pvc(config, TEMPLATE_DIR / "pvc.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["metadata"]["namespace"] == "pypi-cache"

    def test_storage_request_substituted(self):
        config = _default_config()
        result = generate_pvc(config, TEMPLATE_DIR / "pvc.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["resources"]["requests"]["storage"] == "1Ti"

    def test_access_modes_read_write_many(self):
        config = _default_config()
        result = generate_pvc(config, TEMPLATE_DIR / "pvc.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert "ReadWriteMany" in parsed["spec"]["accessModes"]

    def test_storage_class_name(self):
        config = _default_config()
        result = generate_pvc(config, TEMPLATE_DIR / "pvc.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["storageClassName"] == "efs-pypi-cache"

    def test_valid_yaml(self):
        config = _default_config()
        result = generate_pvc(config, TEMPLATE_DIR / "pvc.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["kind"] == "PersistentVolumeClaim"

    def test_no_remaining_placeholders(self):
        config = _default_config()
        result = generate_pvc(config, TEMPLATE_DIR / "pvc.yaml.tpl")
        assert "__" not in result


# ============================================================================
# generate_deployments
# ============================================================================


class TestGenerateDeployments:
    def _parse_docs(self, yaml_str: str) -> list[dict]:
        """Parse multi-doc YAML string into list of dicts."""
        return list(yaml.safe_load_all(yaml_str))

    def test_correct_number_of_documents(self):
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        # cpu + cu121 + cu124 = 3
        assert len(docs) == 3

    def test_all_valid_yaml(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            assert doc["kind"] == "Deployment"
            assert doc["apiVersion"] == "apps/v1"

    def test_no_remaining_placeholders(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        assert not re.search(r"__[A-Z_]+__", result)

    def test_replicas_default(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            assert doc["spec"]["replicas"] == 2

    def test_replicas_override(self):
        config = _default_config()
        config["replicas"] = 5
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            assert doc["spec"]["replicas"] == 5

    # --- Two-container pod structure ---

    def test_two_containers(self):
        """Pod has exactly two containers: nginx and pypiserver."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            containers = doc["spec"]["template"]["spec"]["containers"]
            assert len(containers) == 2
            assert containers[0]["name"] == "nginx"
            assert containers[1]["name"] == "pypiserver"

    def test_nginx_container_image(self):
        """nginx container uses the configured nginx_image."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            assert nginx["image"] == "docker.io/nginxinc/nginx-unprivileged:1.27-alpine"

    def test_nginx_container_port(self):
        """nginx container listens on port 8080 (named 'http')."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            ports = nginx["ports"]
            assert len(ports) == 1
            assert ports[0]["containerPort"] == 8080
            assert ports[0]["name"] == "http"

    def test_pypiserver_container_port(self):
        """pypiserver container listens on internal_port 8081 (named 'pypi-internal')."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            ports = pypiserver["ports"]
            assert len(ports) == 1
            assert ports[0]["containerPort"] == 8081
            assert ports[0]["name"] == "pypi-internal"

    def test_pypiserver_default_image(self):
        """pypiserver container uses the configured image."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            assert pypiserver["image"] == "pypiserver/pypiserver:v2.4.1"

    def test_custom_image(self):
        """Custom image is applied to pypiserver container."""
        config = _default_config()
        config["image"] = "my-registry/pypiserver:custom"
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            assert pypiserver["image"] == "my-registry/pypiserver:custom"

    # --- pypiserver command/args ---

    def test_command_contains_backend_simple_dir(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][1]["args"][0]
            assert "--backend simple-dir" in args

    def test_command_contains_server_gunicorn(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][1]["args"][0]
            assert "--server gunicorn" in args

    def test_command_contains_health_endpoint(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][1]["args"][0]
            assert "--health-endpoint /health" in args

    def test_command_contains_internal_port(self):
        """pypiserver command uses the internal port (8081)."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][1]["args"][0]
            assert "-p 8081" in args

    def test_command_does_not_contain_log_file(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][1]["args"][0]
            assert "--log-file" not in args

    def test_command_pipes_through_log_rotator(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][1]["args"][0]
            assert "log_rotator.py" in args
            assert "--log-dir" in args
            assert "--max-age-days" in args

    # --- Gunicorn env and workers ---

    def test_gunicorn_cmd_args_env(self):
        """pypiserver container has GUNICORN_CMD_ARGS env with workers and timeout."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            env_vars = {e["name"]: e.get("value") for e in pypiserver["env"]}
            assert "GUNICORN_CMD_ARGS" in env_vars
            assert "--workers 4" in env_vars["GUNICORN_CMD_ARGS"]
            assert "--timeout 300" in env_vars["GUNICORN_CMD_ARGS"]

    def test_workers_override(self):
        """Custom workers value is substituted into GUNICORN_CMD_ARGS."""
        config = _default_config()
        config["workers"] = 8
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            env_vars = {e["name"]: e.get("value") for e in pypiserver["env"]}
            assert "--workers 8" in env_vars["GUNICORN_CMD_ARGS"]

    # --- Init containers ---

    def test_init_container_creates_wheelhouse_and_log_dirs(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            init = doc["spec"]["template"]["spec"]["initContainers"][0]
            cmd = " ".join(init["command"])
            slug = doc["metadata"]["labels"]["cuda-version"]
            assert f"/data/wheelhouse/{slug}" in cmd
            assert f"/data/logs/{slug}" in cmd

    # --- Probes (on nginx container, containers[0]) ---

    def test_nginx_readiness_probe(self):
        """nginx container has readiness probe on /health (end-to-end through pypiserver)."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            assert nginx["readinessProbe"]["httpGet"]["path"] == "/health"

    def test_nginx_liveness_probe(self):
        """nginx container has liveness probe on /health."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            assert nginx["livenessProbe"]["httpGet"]["path"] == "/health"

    def test_pypiserver_no_probes(self):
        """pypiserver container has NO probes (nginx handles health checking)."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            assert "readinessProbe" not in pypiserver
            assert "livenessProbe" not in pypiserver

    # --- Affinity ---

    def test_pod_anti_affinity_present(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            affinity = doc["spec"]["template"]["spec"]["affinity"]
            assert "podAntiAffinity" in affinity

    # --- Labels ---

    def test_each_deployment_has_correct_cuda_label(self):
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        expected_slugs = ["cpu", "cu121", "cu124"]
        for doc, slug in zip(docs, expected_slugs, strict=True):
            assert doc["metadata"]["labels"]["cuda-version"] == slug
            assert doc["spec"]["selector"]["matchLabels"]["cuda-version"] == slug
            assert doc["spec"]["template"]["metadata"]["labels"]["cuda-version"] == slug

    # --- Resources: shared nodes (no instance_type) ---

    def test_resource_limits_shared_nodes_nginx(self):
        """Without instance_type, nginx uses fixed config values."""
        config = _default_config()
        config["instance_type"] = ""
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            resources = nginx["resources"]
            assert resources["requests"]["cpu"] == 8
            assert resources["requests"]["memory"] == "2Gi"
            assert resources["limits"]["cpu"] == 8
            assert resources["limits"]["memory"] == "2Gi"

    def test_resource_limits_shared_nodes_pypiserver(self):
        """Without instance_type, pypiserver uses manual server config values."""
        config = _default_config()
        config["instance_type"] = ""
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            resources = pypiserver["resources"]
            assert resources["requests"]["cpu"] == "500m"
            assert resources["requests"]["memory"] == "768Mi"
            assert resources["limits"]["cpu"] == "500m"
            assert resources["limits"]["memory"] == "768Mi"

    # --- Resources: dedicated nodes (r7i.12xlarge) ---

    def test_dedicated_node_selector(self):
        """With instance_type, deployments have nodeSelector for pypi-cache."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            ns = doc["spec"]["template"]["spec"]["nodeSelector"]
            assert ns["workload-type"] == "pypi-cache"
            assert ns["instance-type"] == "r5d.12xlarge"

    def test_dedicated_workload_toleration(self):
        """With instance_type, deployments tolerate workload=pypi-cache taint."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            tolerations = doc["spec"]["template"]["spec"]["tolerations"]
            workload_tol = [t for t in tolerations if t.get("key") == "workload"]
            assert len(workload_tol) == 1
            assert workload_tol[0]["value"] == "pypi-cache"
            assert workload_tol[0]["effect"] == "NoSchedule"

    def test_dedicated_guaranteed_qos_nginx(self):
        """With instance_type, nginx requests == limits (Guaranteed QoS)."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            resources = nginx["resources"]
            assert resources["requests"]["cpu"] == resources["limits"]["cpu"]
            assert resources["requests"]["memory"] == resources["limits"]["memory"]

    def test_dedicated_guaranteed_qos_pypiserver(self):
        """With instance_type, pypiserver requests == limits (Guaranteed QoS)."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            resources = pypiserver["resources"]
            assert resources["requests"]["cpu"] == resources["limits"]["cpu"]
            assert resources["requests"]["memory"] == resources["limits"]["memory"]

    def test_dedicated_computed_resources_total(self):
        """With r5d.12xlarge, total pod resources are 14 vCPU / 105 GiB."""
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            containers = doc["spec"]["template"]["spec"]["containers"]
            nginx_res = containers[0]["resources"]
            server_res = containers[1]["resources"]
            # nginx: 8 vCPU, 2 GiB + pypiserver: 6 vCPU, 103 GiB = 14 vCPU, 105 GiB
            total_cpu = nginx_res["requests"]["cpu"] + server_res["requests"]["cpu"]
            assert total_cpu == 14

    def test_dedicated_computed_resources_nginx(self):
        """With r5d.12xlarge, nginx gets fixed 8 vCPU / 2 GiB."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            resources = nginx["resources"]
            assert resources["requests"]["cpu"] == 8
            assert resources["requests"]["memory"] == "2Gi"

    def test_dedicated_computed_resources_pypiserver(self):
        """With r5d.12xlarge, pypiserver gets remainder: 6 vCPU / 103 GiB."""
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            resources = pypiserver["resources"]
            assert resources["requests"]["cpu"] == 6
            assert resources["requests"]["memory"] == "103Gi"

    def test_dedicated_nginx_exceeds_pod_total_cpu_exits(self):
        """If nginx CPU allocation exceeds pod total, generate_deployments exits."""
        import pytest

        config = _default_config()
        config["nginx"]["cpu"] = 999
        with pytest.raises(SystemExit):
            generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")

    def test_dedicated_nginx_exceeds_pod_total_memory_exits(self):
        """If nginx memory allocation exceeds pod total, generate_deployments exits."""
        import pytest

        config = _default_config()
        config["nginx"]["memory_gi"] = 999
        with pytest.raises(SystemExit):
            generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")

    # --- Shared nodes tolerations ---

    def test_shared_nodes_critical_addons_toleration(self):
        """Without instance_type, deployments tolerate CriticalAddonsOnly."""
        config = _default_config()
        config["instance_type"] = ""
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            tolerations = doc["spec"]["template"]["spec"]["tolerations"]
            ca_tol = [t for t in tolerations if t.get("key") == "CriticalAddonsOnly"]
            assert len(ca_tol) == 1

    def test_shared_nodes_no_node_selector(self):
        """Without instance_type, no nodeSelector is injected."""
        config = _default_config()
        config["instance_type"] = ""
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            assert "nodeSelector" not in doc["spec"]["template"]["spec"]

    # --- Volumes ---

    def test_volumes_nginx_config(self):
        """nginx-config volume is a ConfigMap."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            volumes = doc["spec"]["template"]["spec"]["volumes"]
            vol_map = {v["name"]: v for v in volumes}
            assert "nginx-config" in vol_map
            assert vol_map["nginx-config"]["configMap"]["name"] == "pypi-cache-nginx-config"

    def test_volumes_nginx_cache_nvme_hostpath(self):
        """With NVMe instance, nginx-cache uses hostPath per-slug directories."""
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        expected_slugs = ["cpu", "cu121", "cu124"]
        for doc, slug in zip(docs, expected_slugs, strict=True):
            volumes = doc["spec"]["template"]["spec"]["volumes"]
            vol_map = {v["name"]: v for v in volumes}
            assert "nginx-cache" in vol_map
            assert vol_map["nginx-cache"]["hostPath"]["path"] == f"/mnt/k8s-disks/0/nginx-cache-{slug}"
            assert vol_map["nginx-cache"]["hostPath"]["type"] == "DirectoryOrCreate"

    def test_volumes_nginx_cache_non_nvme_emptydir(self):
        """Without NVMe, nginx-cache falls back to emptyDir."""
        config = _default_config()
        config["instance_type"] = "r7i.12xlarge"
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            volumes = doc["spec"]["template"]["spec"]["volumes"]
            vol_map = {v["name"]: v for v in volumes}
            assert "nginx-cache" in vol_map
            assert vol_map["nginx-cache"]["emptyDir"]["sizeLimit"] == "30Gi"

    def test_volumes_nginx_tmp(self):
        """nginx-tmp volume is emptyDir with Memory medium."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            volumes = doc["spec"]["template"]["spec"]["volumes"]
            vol_map = {v["name"]: v for v in volumes}
            assert "nginx-tmp" in vol_map
            assert vol_map["nginx-tmp"]["emptyDir"]["medium"] == "Memory"

    def test_volumes_pypiserver_tmp(self):
        """pypiserver-tmp volume is emptyDir with Memory medium."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            volumes = doc["spec"]["template"]["spec"]["volumes"]
            vol_map = {v["name"]: v for v in volumes}
            assert "pypiserver-tmp" in vol_map
            assert vol_map["pypiserver-tmp"]["emptyDir"]["medium"] == "Memory"

    def test_nginx_volume_mounts(self):
        """nginx container mounts nginx-config, nginx-cache, and nginx-tmp."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            nginx = doc["spec"]["template"]["spec"]["containers"][0]
            mount_names = [m["name"] for m in nginx["volumeMounts"]]
            assert "nginx-config" in mount_names
            assert "nginx-cache" in mount_names
            assert "nginx-tmp" in mount_names

    def test_pypiserver_volume_mounts(self):
        """pypiserver container mounts data, scripts, and pypiserver-tmp."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            pypiserver = doc["spec"]["template"]["spec"]["containers"][1]
            mount_names = [m["name"] for m in pypiserver["volumeMounts"]]
            assert "data" in mount_names
            assert "scripts" in mount_names
            assert "pypiserver-tmp" in mount_names

    # --- NVMe init container ---

    def test_nvme_init_container_present(self):
        """With NVMe instance (r5d), init-nginx-cache container is generated."""
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            init_containers = doc["spec"]["template"]["spec"]["initContainers"]
            names = [c["name"] for c in init_containers]
            assert "init-nginx-cache" in names
            cache_init = next(c for c in init_containers if c["name"] == "init-nginx-cache")
            assert cache_init["securityContext"]["runAsUser"] == 0
            assert cache_init["securityContext"]["runAsNonRoot"] is False

    def test_non_nvme_no_cache_init_container(self):
        """Without NVMe (r7i), no init-nginx-cache container."""
        config = _default_config()
        config["instance_type"] = "r7i.12xlarge"
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            init_containers = doc["spec"]["template"]["spec"]["initContainers"]
            names = [c["name"] for c in init_containers]
            assert "init-nginx-cache" not in names


# ============================================================================
# generate_services
# ============================================================================


class TestGenerateServices:
    def _parse_docs(self, yaml_str: str) -> list[dict]:
        return list(yaml.safe_load_all(yaml_str))

    def test_correct_number_of_documents(self):
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        docs = self._parse_docs(result)
        assert len(docs) == 3

    def test_all_valid_yaml(self):
        config = _default_config()
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            assert doc["kind"] == "Service"

    def test_service_names_match_slugs(self):
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        docs = self._parse_docs(result)
        expected_names = ["pypi-cache-cpu", "pypi-cache-cu121", "pypi-cache-cu124"]
        actual_names = [doc["metadata"]["name"] for doc in docs]
        assert actual_names == expected_names

    def test_selector_matches_deployment_labels(self):
        config = _default_config()
        config["cuda_versions"] = ["12.1", "12.4"]
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        docs = self._parse_docs(result)
        expected_slugs = ["cpu", "cu121", "cu124"]
        for doc, slug in zip(docs, expected_slugs, strict=True):
            assert doc["spec"]["selector"]["app"] == "pypi-cache"
            assert doc["spec"]["selector"]["cuda-version"] == slug

    def test_no_remaining_placeholders(self):
        config = _default_config()
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        assert not re.search(r"__[A-Z_]+__", result)

    def test_server_port_substituted(self):
        config = _default_config()
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            assert doc["spec"]["ports"][0]["port"] == 8080


# ============================================================================
# list_slugs (CLI behavior via get_slugs)
# ============================================================================


class TestListSlugs:
    def test_list_slugs_default_config(self):
        """The --list-slugs path uses get_slugs; verify it returns expected values."""
        config = _default_config()
        slugs = get_slugs(config)
        assert slugs == ["cpu"]

    def test_list_slugs_custom_cuda(self):
        config = _default_config()
        config["cuda_versions"] = ["11.8"]
        slugs = get_slugs(config)
        assert slugs == ["cpu", "cu118"]

    def test_list_slugs_via_load_config(self, tmp_path: Path):
        """End-to-end: load_config + get_slugs mirrors --list-slugs output."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  cuda_versions:
                    - "12.6"
                    - "12.8"
            """,
        )
        config = load_config(cy, "test-cluster")
        slugs = get_slugs(config)
        assert slugs == ["cpu", "cu126", "cu128"]


# ============================================================================
# compute_pod_resources
# ============================================================================


class TestComputePodResources:
    def test_r7i_12xlarge_3_pods(self):
        """r7i.12xlarge with 3 pods/node: 14 vCPU, 105 GiB per pod."""
        res = compute_pod_resources("r7i.12xlarge", 3)
        assert res["cpu"] == 14
        assert res["memory_gi"] == 105

    def test_r7i_2xlarge_3_pods(self):
        """r7i.2xlarge with 3 pods/node: 2 vCPU, 17 GiB per pod."""
        res = compute_pod_resources("r7i.2xlarge", 3)
        assert res["cpu"] == 2
        assert res["memory_gi"] == 17

    def test_guaranteed_qos_truncation(self):
        """Values are truncated to whole vCPU and GiB (floor division)."""
        res = compute_pod_resources("r7i.12xlarge", 3)
        # cpu is floor(pod_cpu_m / 1000), memory_gi is floor(pod_mem_mi / 1024)
        assert isinstance(res["cpu"], int)
        assert isinstance(res["memory_gi"], int)

    def test_different_pods_per_node(self):
        """More pods per node = less resources per pod."""
        res_3 = compute_pod_resources("r7i.12xlarge", 3)
        res_6 = compute_pod_resources("r7i.12xlarge", 6)
        assert res_3["cpu"] > res_6["cpu"] or res_3["cpu"] == res_6["cpu"]
        assert res_3["memory_gi"] > res_6["memory_gi"]

    def test_allocatable_in_result(self):
        """Result includes allocatable values for diagnostics."""
        res = compute_pod_resources("r7i.12xlarge", 3)
        assert "allocatable_cpu_m" in res
        assert "allocatable_mem_mi" in res
        assert res["allocatable_cpu_m"] > 0
        assert res["allocatable_mem_mi"] > 0

    def test_unknown_instance_type_raises(self):
        """Unknown instance type raises KeyError."""
        import pytest

        with pytest.raises(KeyError):
            compute_pod_resources("z99.nonexistent", 2)


# ============================================================================
# compute_nginx_cache_size
# ============================================================================


class TestComputeNginxCacheSize:
    def test_r5d_12xlarge_3_pods(self):
        """r5d.12xlarge (1800 GiB NVMe) with 3 pods: floor(1800 * 0.95 / 3) = 570."""
        result = compute_nginx_cache_size("r5d.12xlarge", 3)
        assert result == 570

    def test_non_nvme_returns_none(self):
        """Instance without NVMe returns None."""
        result = compute_nginx_cache_size("r7i.12xlarge", 1)
        assert result is None

    def test_scales_with_pods_per_node(self):
        """More pods per node = less cache per pod."""
        result_1 = compute_nginx_cache_size("r5d.12xlarge", 1)
        result_3 = compute_nginx_cache_size("r5d.12xlarge", 3)
        result_6 = compute_nginx_cache_size("r5d.12xlarge", 6)
        assert result_1 is not None
        assert result_3 is not None
        assert result_6 is not None
        assert result_1 > result_3 > result_6


# ============================================================================
# generate_nodepool
# ============================================================================


class TestGenerateNodepool:
    def test_valid_yaml(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["kind"] == "NodePool"
        assert parsed["apiVersion"] == "karpenter.sh/v1"

    def test_no_remaining_placeholders(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        assert not re.search(r"__[A-Z_]+__", result)

    def test_instance_type_in_requirements(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        reqs = parsed["spec"]["template"]["spec"]["requirements"]
        instance_req = [r for r in reqs if r["key"] == "node.kubernetes.io/instance-type"]
        assert len(instance_req) == 1
        assert "r5d.12xlarge" in instance_req[0]["values"]

    def test_workload_type_label(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        labels = parsed["spec"]["template"]["metadata"]["labels"]
        assert labels["workload-type"] == "pypi-cache"

    def test_workload_taint(self):
        """NodePool taint is workload=pypi-cache (decoupled from instance type)."""
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        taints = parsed["spec"]["template"]["spec"]["taints"]
        workload_taint = [t for t in taints if t["key"] == "workload"]
        assert len(workload_taint) == 1
        assert workload_taint[0]["value"] == "pypi-cache"
        assert workload_taint[0]["effect"] == "NoSchedule"

    def test_no_startup_taints(self):
        """pypi-cache nodes have no startup taints (unlike runner/buildkit nodes)."""
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        assert "startupTaints" not in result

    def test_on_demand_only(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        reqs = parsed["spec"]["template"]["spec"]["requirements"]
        cap_type = [r for r in reqs if r["key"] == "karpenter.sh/capacity-type"]
        assert len(cap_type) == 1
        assert cap_type[0]["values"] == ["on-demand"]

    def test_amd64_only(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        reqs = parsed["spec"]["template"]["spec"]["requirements"]
        arch = [r for r in reqs if r["key"] == "kubernetes.io/arch"]
        assert len(arch) == 1
        assert arch[0]["values"] == ["amd64"]

    def test_cpu_limit_computed(self):
        """CPU limit = max_nodes * vcpu = (replicas * 2) * 48 = 192."""
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        # Default replicas=2, r5d.12xlarge=48 vCPU, max_nodes=4
        assert parsed["spec"]["limits"]["cpu"] == "192"

    def test_memory_limit_computed(self):
        """Memory limit = max_nodes * memory_gib = (replicas * 2) * 384 = 1536Gi."""
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["limits"]["memory"] == "1536Gi"

    def test_disruption_when_empty(self):
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["disruption"]["consolidationPolicy"] == "WhenEmpty"

    def test_disruption_budget_zero(self):
        """Budget of 0 means no voluntary disruption."""
        config = _default_config()
        result = generate_nodepool(config, TEMPLATE_DIR / "nodepool.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["disruption"]["budgets"][0]["nodes"] == "0"


# ============================================================================
# generate_ec2nodeclass
# ============================================================================


class TestGenerateEc2NodeClass:
    def test_valid_yaml(self):
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["kind"] == "EC2NodeClass"
        assert parsed["apiVersion"] == "karpenter.k8s.aws/v1"

    def test_cluster_name_placeholder_preserved(self):
        """CLUSTER_NAME_PLACEHOLDER is left for sed substitution at deploy time."""
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        assert "CLUSTER_NAME_PLACEHOLDER" in result

    def test_instance_store_policy_nvme(self):
        """r5d has NVMe — instanceStorePolicy: RAID0 for Karpenter auto-discovery."""
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["instanceStorePolicy"] == "RAID0"

    def test_no_instance_store_policy_non_nvme(self):
        """Non-NVMe instance has no instanceStorePolicy."""
        config = _default_config()
        config["instance_type"] = "r7i.12xlarge"
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert "instanceStorePolicy" not in parsed["spec"]

    def test_no_cpu_manager_policy(self):
        """pypiserver is I/O-bound — no cpuManagerPolicy (no CPU pinning)."""
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        assert "cpuManagerPolicy" not in result

    def test_instance_type_in_tags(self):
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["tags"]["InstanceType"] == "r5d.12xlarge"

    def test_no_remaining_generator_placeholders(self):
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        assert not re.search(r"__[A-Z_]+__", result)

    def test_imdsv2_required(self):
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        parsed = yaml.safe_load(result)
        assert parsed["spec"]["metadataOptions"]["httpTokens"] == "required"

    def test_volume_size_200gi(self):
        """EC2NodeClass EBS volume is 200Gi."""
        config = _default_config()
        result = generate_ec2nodeclass(config, TEMPLATE_DIR / "ec2nodeclass.yaml.tpl")
        parsed = yaml.safe_load(result)
        block_devices = parsed["spec"]["blockDeviceMappings"]
        assert block_devices[0]["ebs"]["volumeSize"] == "200Gi"


# ============================================================================
# generate_nodepools (combined)
# ============================================================================


class TestGenerateNodepools:
    def test_produces_two_documents(self):
        """Combined output has NodePool + EC2NodeClass."""
        config = _default_config()
        result = generate_nodepools(config, TEMPLATE_DIR)
        docs = list(yaml.safe_load_all(result))
        assert len(docs) == 2

    def test_document_kinds(self):
        config = _default_config()
        result = generate_nodepools(config, TEMPLATE_DIR)
        docs = list(yaml.safe_load_all(result))
        kinds = [doc["kind"] for doc in docs]
        assert "NodePool" in kinds
        assert "EC2NodeClass" in kinds


# ============================================================================
# log_info
# ============================================================================


class TestLogInfo:
    def test_log_info_prints_to_stdout(self, capsys):
        log_info("hello world")
        captured = capsys.readouterr()
        assert "hello world" in captured.out
        # Contains the ANSI arrow character
        assert "\u2192" in captured.out


# ============================================================================
# main() — CLI entrypoint
# ============================================================================


class TestMain:
    def _clusters_yaml(self, tmp_path: Path) -> Path:
        """Write a minimal clusters.yaml and return its path."""
        return _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  cuda_versions:
                    - "12.1"
            """,
        )

    def test_list_slugs_mode(self, tmp_path: Path, capsys):
        """--list-slugs prints slugs to stdout and returns 0."""
        cy = self._clusters_yaml(tmp_path)
        with patch(
            "sys.argv",
            ["generate_manifests.py", "--cluster", "test-cluster", "--clusters-yaml", str(cy), "--list-slugs"],
        ):
            ret = main()
        assert ret == 0
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert "cpu" in lines
        assert "cu121" in lines

    def test_full_generation(self, tmp_path: Path):
        """Full manifest generation writes all four files and returns 0."""
        cy = self._clusters_yaml(tmp_path)
        output_dir = tmp_path / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-abc123",
                "--output-dir",
                str(output_dir),
            ],
        ):
            ret = main()

        assert ret == 0
        assert (output_dir / "storageclass.yaml").exists()
        assert (output_dir / "pvc.yaml").exists()
        assert (output_dir / "deployments.yaml").exists()
        assert (output_dir / "services.yaml").exists()

    def test_full_generation_creates_output_dir(self, tmp_path: Path):
        """Output directory is created if it doesn't exist."""
        cy = self._clusters_yaml(tmp_path)
        output_dir = tmp_path / "nested" / "dir" / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-test",
                "--output-dir",
                str(output_dir),
            ],
        ):
            ret = main()

        assert ret == 0
        assert output_dir.is_dir()

    def test_missing_efs_id_without_list_slugs(self, tmp_path: Path):
        """--efs-filesystem-id is required when not using --list-slugs."""
        cy = self._clusters_yaml(tmp_path)
        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--output-dir",
                str(tmp_path / "out"),
            ],
        ):
            import pytest

            with pytest.raises(SystemExit):
                main()

    def test_missing_output_dir_without_list_slugs(self, tmp_path: Path):
        """--output-dir is required when not using --list-slugs."""
        cy = self._clusters_yaml(tmp_path)
        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-abc123",
            ],
        ):
            import pytest

            with pytest.raises(SystemExit):
                main()

    def test_generated_storageclass_content(self, tmp_path: Path):
        """Generated storageclass.yaml contains the EFS filesystem ID."""
        cy = self._clusters_yaml(tmp_path)
        output_dir = tmp_path / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-verify123",
                "--output-dir",
                str(output_dir),
            ],
        ):
            main()

        sc_content = (output_dir / "storageclass.yaml").read_text()
        assert "fs-verify123" in sc_content
        assert "__EFS_FILESYSTEM_ID__" not in sc_content

    def test_generated_deployments_content(self, tmp_path: Path):
        """Generated deployments.yaml contains expected CUDA slugs."""
        cy = self._clusters_yaml(tmp_path)
        output_dir = tmp_path / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-test",
                "--output-dir",
                str(output_dir),
            ],
        ):
            main()

        deploy_content = (output_dir / "deployments.yaml").read_text()
        docs = list(yaml.safe_load_all(deploy_content))
        slugs = [doc["metadata"]["labels"]["cuda-version"] for doc in docs]
        assert slugs == ["cpu", "cu121"]

    def test_nodepools_yaml_generated_with_instance_type(self, tmp_path: Path):
        """When instance_type is configured, nodepools.yaml is generated."""
        cy = self._clusters_yaml(tmp_path)
        output_dir = tmp_path / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-test",
                "--output-dir",
                str(output_dir),
            ],
        ):
            ret = main()

        assert ret == 0
        assert (output_dir / "nodepools.yaml").exists()
        content = (output_dir / "nodepools.yaml").read_text()
        docs = list(yaml.safe_load_all(content))
        kinds = [doc["kind"] for doc in docs]
        assert "NodePool" in kinds
        assert "EC2NodeClass" in kinds

    def test_no_nodepools_yaml_without_instance_type(self, tmp_path: Path):
        """When instance_type is empty, nodepools.yaml is NOT generated."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  instance_type: ""
                  cuda_versions:
                    - "12.1"
            """,
        )
        output_dir = tmp_path / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-test",
                "--output-dir",
                str(output_dir),
            ],
        ):
            ret = main()

        assert ret == 0
        assert not (output_dir / "nodepools.yaml").exists()

    def test_print_nginx_max_cache_size_nvme_output(self, tmp_path: Path, capsys):
        """--print-nginx-max-cache-size with NVMe instance prints computed size."""
        cy = self._clusters_yaml(tmp_path)
        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--print-nginx-max-cache-size",
            ],
        ):
            ret = main()
        assert ret == 0
        output = capsys.readouterr().out.strip()
        # r5d.12xlarge with 2 slugs (cpu + cu121): floor(1800 * 0.95 / 2) = 855
        assert output == "855g"

    def test_print_nginx_max_cache_size_non_nvme(self, tmp_path: Path, capsys):
        """--print-nginx-max-cache-size without NVMe prints fallback from cache_size."""
        cy = _write_clusters_yaml(
            tmp_path,
            """\
            clusters:
              test-cluster:
                region: us-west-2
                pypi_cache:
                  instance_type: "r7i.12xlarge"
                  cuda_versions:
                    - "12.1"
            """,
        )
        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--print-nginx-max-cache-size",
            ],
        ):
            ret = main()
        assert ret == 0
        output = capsys.readouterr().out.strip()
        # Default cache_size is 30Gi, so max_size = 30 - 5 = 25g
        assert output == "25g"

    def test_instance_type_cli_override(self, tmp_path: Path):
        """--instance-type CLI arg overrides config."""
        cy = self._clusters_yaml(tmp_path)
        output_dir = tmp_path / "output"

        with patch(
            "sys.argv",
            [
                "generate_manifests.py",
                "--cluster",
                "test-cluster",
                "--clusters-yaml",
                str(cy),
                "--efs-filesystem-id",
                "fs-test",
                "--output-dir",
                str(output_dir),
                "--instance-type",
                "",
            ],
        ):
            ret = main()

        assert ret == 0
        # Empty instance-type override means no nodepools
        assert not (output_dir / "nodepools.yaml").exists()
