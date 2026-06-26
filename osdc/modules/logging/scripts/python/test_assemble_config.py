"""Unit tests for assemble_config.py."""

import pytest
import yaml
from assemble_config import main, parse_args, render_configmap


class TestRenderConfigmap:
    def test_configmap_yaml_valid(self):
        result = render_configmap("// alloy config", "logging")
        parsed = yaml.safe_load(result)
        assert parsed["apiVersion"] == "v1"
        assert parsed["kind"] == "ConfigMap"
        assert parsed["metadata"]["name"] == "alloy-logging-config"
        assert parsed["metadata"]["namespace"] == "logging"
        assert parsed["data"]["config.alloy"] == "// alloy config"

    def test_configmap_custom_name(self):
        result = render_configmap("// alloy config", "logging", name="custom-name")
        parsed = yaml.safe_load(result)
        assert parsed["metadata"]["name"] == "custom-name"

    def test_configmap_preserves_multiline_config(self):
        config = '// line 1\nloki.write "x" {\n    url = "http://..."\n}\n'
        result = render_configmap(config, "logging")
        parsed = yaml.safe_load(result)
        assert parsed["data"]["config.alloy"] == config


class TestParseArgs:
    def test_required_args(self):
        args = parse_args(["--base-pipeline", "base.alloy", "--namespace", "logging", "--output", "out.yaml"])
        assert args.base_pipeline == "base.alloy"
        assert args.namespace == "logging"
        assert args.output == "out.yaml"

    def test_missing_required_arg_exits(self):
        with pytest.raises(SystemExit):
            parse_args(["--base-pipeline", "base.alloy"])


class TestMain:
    def test_main_writes_configmap(self, tmp_path):
        base = tmp_path / "base.alloy"
        base.write_text("// alloy content\n")
        out = tmp_path / "out" / "cm.yaml"

        main(["--base-pipeline", str(base), "--namespace", "logging", "--output", str(out)])

        assert out.is_file()
        parsed = yaml.safe_load(out.read_text())
        assert parsed["data"]["config.alloy"] == "// alloy content\n"
        assert parsed["metadata"]["namespace"] == "logging"

    def test_main_missing_base_pipeline_exits(self, tmp_path, capsys):
        out = tmp_path / "out.yaml"
        with pytest.raises(SystemExit) as exc:
            main(["--base-pipeline", str(tmp_path / "nope.alloy"), "--namespace", "logging", "--output", str(out)])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not found" in err
