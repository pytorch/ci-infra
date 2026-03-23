"""Unit tests for assemble_config.py."""

import textwrap

import yaml
from assemble_config import assemble_config, discover_pipeline, load_cluster_modules, render_configmap

SAMPLE_BASE = textwrap.dedent("""\
    loki.process "default" {
        // MODULE_PIPELINES
        stage.static_labels {
            values = { cluster = "test" }
        }
    }
""")

SAMPLE_CLUSTERS_YAML = textwrap.dedent("""\
    defaults: {}
    clusters:
        test-cluster:
            cluster_name: pytorch-test
            region: us-east-1
            modules:
                - karpenter
                - monitoring
                - arc
""")


def _write_clusters_yaml(tmp_path, content=SAMPLE_CLUSTERS_YAML):
    p = tmp_path / "clusters.yaml"
    p.write_text(content)
    return p


def _write_pipeline(base_dir, module_name, content):
    pipeline_dir = base_dir / module_name / "logging"
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    pipeline_file = pipeline_dir / "pipeline.alloy"
    pipeline_file.write_text(content)
    return pipeline_file


class TestLoadClusterModules:
    def test_load_cluster_modules(self, tmp_path):
        clusters_yaml = _write_clusters_yaml(tmp_path)
        modules = load_cluster_modules(clusters_yaml, "test-cluster")
        assert modules == ["karpenter", "monitoring", "arc"]

    def test_load_cluster_modules_empty(self, tmp_path):
        content = textwrap.dedent("""\
            clusters:
                empty-cluster:
                    cluster_name: pytorch-empty
                    region: us-west-2
        """)
        clusters_yaml = _write_clusters_yaml(tmp_path, content)
        modules = load_cluster_modules(clusters_yaml, "empty-cluster")
        assert modules == []


class TestDiscoverPipeline:
    def test_consumer_overrides_upstream(self, tmp_path):
        consumer_dir = tmp_path / "consumer" / "modules"
        upstream_dir = tmp_path / "upstream" / "modules"

        _write_pipeline(consumer_dir, "monitoring", "// consumer pipeline\nstage.drop {}")
        _write_pipeline(upstream_dir, "monitoring", "// upstream pipeline\nstage.json {}")

        result = discover_pipeline("monitoring", consumer_dir, upstream_dir)
        assert result is not None
        assert "consumer pipeline" in result
        assert "upstream pipeline" not in result

    def test_falls_back_to_upstream(self, tmp_path):
        consumer_dir = tmp_path / "consumer" / "modules"
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)

        _write_pipeline(upstream_dir, "arc", "// upstream arc pipeline")

        result = discover_pipeline("arc", consumer_dir, upstream_dir)
        assert result is not None
        assert "upstream arc pipeline" in result

    def test_module_without_pipeline_file(self, tmp_path):
        consumer_dir = tmp_path / "consumer" / "modules"
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)
        upstream_dir.mkdir(parents=True, exist_ok=True)

        result = discover_pipeline("nonexistent", consumer_dir, upstream_dir)
        assert result is None

    def test_empty_pipeline_file_skipped(self, tmp_path):
        consumer_dir = tmp_path / "consumer" / "modules"
        upstream_dir = tmp_path / "upstream" / "modules"
        upstream_dir.mkdir(parents=True, exist_ok=True)

        _write_pipeline(consumer_dir, "karpenter", "   \n  \n")

        result = discover_pipeline("karpenter", consumer_dir, upstream_dir)
        assert result is None

    def test_empty_consumer_suppresses_upstream(self, tmp_path):
        """An empty consumer pipeline file opts out of the upstream pipeline entirely."""
        consumer_dir = tmp_path / "consumer" / "modules"
        upstream_dir = tmp_path / "upstream" / "modules"

        _write_pipeline(consumer_dir, "karpenter", "   \n")
        _write_pipeline(upstream_dir, "karpenter", "// upstream karpenter pipeline")

        result = discover_pipeline("karpenter", consumer_dir, upstream_dir)
        assert result is None


class TestAssembleConfig:
    def test_base_only_no_modules(self):
        result = assemble_config(SAMPLE_BASE, {})
        assert "MODULE_PIPELINES" not in result
        assert "stage.static_labels" in result
        assert "loki.process" in result

    def test_missing_marker_with_modules_exits(self):
        """assemble_config must exit with an error when the marker is absent but modules exist."""
        import pytest

        base_without_marker = textwrap.dedent("""\
            loki.process "default" {
                stage.static_labels {
                    values = { cluster = "test" }
                }
            }
        """)
        with pytest.raises(SystemExit) as exc_info:
            assemble_config(base_without_marker, {"monitoring": "stage.json {}"})
        assert exc_info.value.code == 1

    def test_missing_marker_no_modules_is_ok(self):
        """assemble_config must NOT exit when the marker is absent and there are no modules."""
        base_without_marker = textwrap.dedent("""\
            loki.process "default" {
                stage.static_labels {
                    values = { cluster = "test" }
                }
            }
        """)
        # Should not raise
        result = assemble_config(base_without_marker, {})
        assert "stage.static_labels" in result

    def test_module_with_pipeline(self):
        pipelines = {"monitoring": 'stage.json {\n    expressions = { level = "" }\n}'}
        result = assemble_config(SAMPLE_BASE, pipelines)
        assert "// --- module: monitoring ---" in result
        assert "// --- end module: monitoring ---" in result
        assert "stage.json" in result
        assert "MODULE_PIPELINES" not in result

    def test_marker_fully_removed(self):
        pipelines = {"arc": "stage.drop {}"}
        result = assemble_config(SAMPLE_BASE, pipelines)
        assert "MODULE_PIPELINES" not in result

    def test_multiple_modules_order(self):
        pipelines = {
            "karpenter": "// karpenter stage",
            "monitoring": "// monitoring stage",
            "arc": "// arc stage",
        }
        result = assemble_config(SAMPLE_BASE, pipelines)

        karpenter_pos = result.index("module: karpenter")
        monitoring_pos = result.index("module: monitoring")
        arc_pos = result.index("module: arc")

        assert karpenter_pos < monitoring_pos < arc_pos

    def test_module_content_indented(self):
        pipelines = {"arc": "stage.drop {\n    selector = {}\n}"}
        result = assemble_config(SAMPLE_BASE, pipelines)
        lines = result.splitlines()
        # Find lines with stage.drop — they should be indented by 4 spaces
        stage_lines = [ln for ln in lines if "stage.drop" in ln]
        assert len(stage_lines) == 1
        assert stage_lines[0].startswith("    ")


class TestRenderConfigmap:
    def test_configmap_yaml_valid(self):
        config_content = "some alloy config content"
        result = render_configmap(config_content, "logging")
        parsed = yaml.safe_load(result)

        assert parsed["apiVersion"] == "v1"
        assert parsed["kind"] == "ConfigMap"
        assert parsed["metadata"]["name"] == "alloy-logging-config"
        assert parsed["metadata"]["namespace"] == "logging"
        assert parsed["data"]["config.alloy"] == config_content

    def test_configmap_custom_name(self):
        result = render_configmap("content", "test-ns", name="my-config")
        parsed = yaml.safe_load(result)
        assert parsed["metadata"]["name"] == "my-config"
        assert parsed["metadata"]["namespace"] == "test-ns"


class TestIntegration:
    def test_module_not_in_cluster_list(self, tmp_path):
        """Module with a pipeline file but not in cluster's module list is excluded."""
        clusters_yaml = _write_clusters_yaml(tmp_path)
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir = tmp_path / "consumer" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)

        # Create pipeline for a module NOT in the cluster's module list
        _write_pipeline(upstream_dir, "buildkit", "// buildkit pipeline")
        # Also create one that IS in the list
        _write_pipeline(upstream_dir, "monitoring", "// monitoring pipeline")

        modules = load_cluster_modules(clusters_yaml, "test-cluster")

        module_pipelines = {}
        for mod_name in modules:
            content = discover_pipeline(mod_name, consumer_dir, upstream_dir)
            if content is not None:
                module_pipelines[mod_name] = content

        result = assemble_config(SAMPLE_BASE, module_pipelines)

        assert "monitoring pipeline" in result
        assert "buildkit pipeline" not in result

    def test_full_pipeline(self, tmp_path):
        """End-to-end: clusters.yaml -> discover -> assemble -> render ConfigMap."""
        clusters_yaml = _write_clusters_yaml(tmp_path)
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir = tmp_path / "consumer" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)

        _write_pipeline(upstream_dir, "monitoring", 'stage.json {\n    expressions = { msg = "" }\n}')

        modules = load_cluster_modules(clusters_yaml, "test-cluster")
        module_pipelines = {}
        for mod_name in modules:
            content = discover_pipeline(mod_name, consumer_dir, upstream_dir)
            if content is not None:
                module_pipelines[mod_name] = content

        assembled = assemble_config(SAMPLE_BASE, module_pipelines)
        configmap_yaml = render_configmap(assembled, "logging")

        parsed = yaml.safe_load(configmap_yaml)
        config_content = parsed["data"]["config.alloy"]
        assert "MODULE_PIPELINES" not in config_content
        assert "module: monitoring" in config_content
        assert "stage.json" in config_content
        assert "stage.static_labels" in config_content
