"""Unit tests for assemble_config.py."""

import textwrap
from unittest.mock import patch

import yaml
from assemble_config import assemble_config, discover_pipeline, load_cluster_modules, main, render_configmap

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

    def test_unknown_cluster_exits(self, tmp_path):
        """load_cluster_modules exits with error when cluster ID is not found."""
        import pytest

        clusters_yaml = _write_clusters_yaml(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            load_cluster_modules(clusters_yaml, "nonexistent-cluster")
        assert exc_info.value.code == 1


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

    def test_blank_lines_in_pipeline_preserved(self):
        """Blank lines in module pipeline content are emitted as bare newlines."""
        pipelines = {"arc": "stage.drop {\n\n    selector = {}\n}"}
        result = assemble_config(SAMPLE_BASE, pipelines)
        lines = result.splitlines(keepends=True)
        # Find the blank line between "stage.drop {" and "    selector = {}"
        stage_drop_idx = next(i for i, ln in enumerate(lines) if "stage.drop" in ln)
        # The line immediately after "stage.drop {" should be a bare newline
        assert lines[stage_drop_idx + 1] == "\n"


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

    def test_logging_module_skipped_in_discovery(self, tmp_path):
        """The 'logging' module is skipped during pipeline discovery to avoid self-reference."""
        content = textwrap.dedent("""\
            defaults: {}
            clusters:
                test-cluster:
                    cluster_name: pytorch-test
                    region: us-east-1
                    modules:
                        - karpenter
                        - logging
                        - monitoring
        """)
        clusters_yaml = _write_clusters_yaml(tmp_path, content)
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir = tmp_path / "consumer" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)

        _write_pipeline(upstream_dir, "monitoring", "// monitoring pipeline")
        # Create a pipeline file at the self-referencing path that would be found
        # if 'logging' were not skipped (modules/logging/logging/pipeline.alloy)
        _write_pipeline(upstream_dir, "logging", "// this should never be included")

        modules = load_cluster_modules(clusters_yaml, "test-cluster")
        assert "logging" in modules

        module_pipelines = {}
        for mod_name in modules:
            if mod_name == "logging":
                continue
            pipeline_content = discover_pipeline(mod_name, consumer_dir, upstream_dir)
            if pipeline_content is not None:
                module_pipelines[mod_name] = pipeline_content

        # 'logging' must not appear in discovered pipelines
        assert "logging" not in module_pipelines
        # Other modules must still be discovered normally
        assert "monitoring" in module_pipelines
        assert "monitoring pipeline" in module_pipelines["monitoring"]

        result = assemble_config(SAMPLE_BASE, module_pipelines)
        assert "this should never be included" not in result
        assert "monitoring pipeline" in result

    def test_main_skips_logging_module(self, tmp_path):
        """main() must never pass 'logging' to discover_pipeline(), even when it appears in clusters.yaml."""
        content = textwrap.dedent("""\
            defaults: {}
            clusters:
                test-cluster:
                    cluster_name: pytorch-test
                    region: us-east-1
                    modules:
                        - karpenter
                        - logging
                        - monitoring
        """)
        clusters_yaml = _write_clusters_yaml(tmp_path, content)
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir = tmp_path / "consumer" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)

        _write_pipeline(upstream_dir, "monitoring", "// monitoring pipeline")
        _write_pipeline(upstream_dir, "karpenter", "// karpenter pipeline")
        # Place a file at the self-referencing path so we can detect if it leaks through
        _write_pipeline(upstream_dir, "logging", "// logging self-ref — must not appear")

        base_path = tmp_path / "base.alloy"
        base_path.write_text(SAMPLE_BASE)
        output_path = tmp_path / "output" / "config.yaml"

        with patch("assemble_config.discover_pipeline", wraps=discover_pipeline) as mock_discover:
            main(
                [
                    "--base-pipeline",
                    str(base_path),
                    "--modules-dir",
                    str(consumer_dir),
                    "--upstream-modules-dir",
                    str(upstream_dir),
                    "--cluster",
                    "test-cluster",
                    "--clusters-yaml",
                    str(clusters_yaml),
                    "--namespace",
                    "logging",
                    "--output",
                    str(output_path),
                ]
            )

            # discover_pipeline must never be called with 'logging'
            called_modules = [call.args[0] for call in mock_discover.call_args_list]
            assert "logging" not in called_modules
            # Other modules must still be discovered
            assert "karpenter" in called_modules
            assert "monitoring" in called_modules

        # Verify the output file was written and excludes the logging self-reference
        result = output_path.read_text()
        assert "logging self-ref" not in result
        assert "monitoring pipeline" in result
        assert "karpenter pipeline" in result

    def test_main_exits_when_base_pipeline_missing(self, tmp_path):
        """main() exits with error when the base pipeline file does not exist."""
        import pytest

        clusters_yaml = _write_clusters_yaml(tmp_path)
        consumer_dir = tmp_path / "consumer" / "modules"
        upstream_dir = tmp_path / "upstream" / "modules"
        consumer_dir.mkdir(parents=True, exist_ok=True)
        upstream_dir.mkdir(parents=True, exist_ok=True)
        output_path = tmp_path / "output" / "config.yaml"
        nonexistent_base = tmp_path / "does-not-exist.alloy"

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--base-pipeline",
                    str(nonexistent_base),
                    "--modules-dir",
                    str(consumer_dir),
                    "--upstream-modules-dir",
                    str(upstream_dir),
                    "--cluster",
                    "test-cluster",
                    "--clusters-yaml",
                    str(clusters_yaml),
                    "--namespace",
                    "logging",
                    "--output",
                    str(output_path),
                ]
            )
        assert exc_info.value.code == 1

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
