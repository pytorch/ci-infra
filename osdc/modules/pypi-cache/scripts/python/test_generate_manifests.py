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
    cuda_slug,
    generate_deployments,
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
        assert slugs == ["cpu", "cu121", "cu124"]

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
        assert config["cuda_versions"] == ["12.1", "12.4"]

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
                    cpu_limit: "2"
            """,
        )
        config = load_config(cy, "test-cluster")
        # Overridden field
        assert config["server"]["cpu_limit"] == "2"
        # Non-overridden fields preserved from DEFAULTS
        assert config["server"]["cpu_request"] == "100m"
        assert config["server"]["memory_request"] == "256Mi"
        assert config["server"]["memory_limit"] == "512Mi"

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
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        # Default: cpu + cu121 + cu124 = 3
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

    def test_command_contains_backend_simple_dir(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][0]["args"][0]
            assert "--backend simple-dir" in args

    def test_command_contains_server_gunicorn(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][0]["args"][0]
            assert "--server gunicorn" in args

    def test_command_contains_health_endpoint(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][0]["args"][0]
            assert "--health-endpoint /health" in args

    def test_command_does_not_contain_log_file(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][0]["args"][0]
            assert "--log-file" not in args

    def test_command_pipes_through_log_rotator(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            args = doc["spec"]["template"]["spec"]["containers"][0]["args"][0]
            assert "log_rotator.py" in args
            assert "--log-dir" in args
            assert "--max-age-days" in args

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

    def test_probes_target_health_endpoint(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            container = doc["spec"]["template"]["spec"]["containers"][0]
            assert container["readinessProbe"]["httpGet"]["path"] == "/health"
            assert container["livenessProbe"]["httpGet"]["path"] == "/health"

    def test_default_image(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            image = doc["spec"]["template"]["spec"]["containers"][0]["image"]
            assert image == "pypiserver/pypiserver:v2.4.1"

    def test_pod_anti_affinity_present(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            affinity = doc["spec"]["template"]["spec"]["affinity"]
            assert "podAntiAffinity" in affinity

    def test_each_deployment_has_correct_cuda_label(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        expected_slugs = ["cpu", "cu121", "cu124"]
        for doc, slug in zip(docs, expected_slugs, strict=True):
            assert doc["metadata"]["labels"]["cuda-version"] == slug
            assert doc["spec"]["selector"]["matchLabels"]["cuda-version"] == slug
            assert doc["spec"]["template"]["metadata"]["labels"]["cuda-version"] == slug

    def test_resource_limits_substituted(self):
        config = _default_config()
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            resources = doc["spec"]["template"]["spec"]["containers"][0]["resources"]
            assert resources["requests"]["cpu"] == "100m"
            assert resources["requests"]["memory"] == "256Mi"
            assert resources["limits"]["cpu"] == "500m"
            assert resources["limits"]["memory"] == "512Mi"

    def test_custom_image(self):
        config = _default_config()
        config["image"] = "my-registry/pypiserver:custom"
        result = generate_deployments(config, TEMPLATE_DIR / "deployment.yaml.tpl")
        docs = self._parse_docs(result)
        for doc in docs:
            image = doc["spec"]["template"]["spec"]["containers"][0]["image"]
            assert image == "my-registry/pypiserver:custom"


# ============================================================================
# generate_services
# ============================================================================


class TestGenerateServices:
    def _parse_docs(self, yaml_str: str) -> list[dict]:
        return list(yaml.safe_load_all(yaml_str))

    def test_correct_number_of_documents(self):
        config = _default_config()
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
        result = generate_services(config, TEMPLATE_DIR / "service.yaml.tpl")
        docs = self._parse_docs(result)
        expected_names = ["pypi-cache-cpu", "pypi-cache-cu121", "pypi-cache-cu124"]
        actual_names = [doc["metadata"]["name"] for doc in docs]
        assert actual_names == expected_names

    def test_selector_matches_deployment_labels(self):
        config = _default_config()
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
        assert slugs == ["cpu", "cu121", "cu124"]

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
