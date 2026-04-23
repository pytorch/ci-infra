"""Tests for capacity_setter module."""

import argparse
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from capacity_setter import (
    CONFIGMAP_PREFIX,
    cmd_delete,
    cmd_get,
    cmd_list,
    cmd_set,
    cmd_watch,
    configmap_name,
    ensure_kubeconfig,
    kubectl_env,
    load_clusters_yaml,
    main,
    normalize_name,
    parse_args,
    run_kubectl,
    validate_runner_name,
)

FAKE_CLUSTERS = {
    "clusters": {
        "arc-staging": {
            "cluster_name": "pytorch-arc-staging",
            "region": "us-west-1",
        },
    },
}

CTX = "pytorch-arc-staging"


class TestNormalizeName:
    def test_no_change(self):
        assert normalize_name("l-x86iavx512-8-16") == "l-x86iavx512-8-16"

    def test_dots_to_dashes(self):
        assert normalize_name("runner.v2.big") == "runner-v2-big"

    def test_underscores_to_dashes(self):
        assert normalize_name("runner_v2_big") == "runner-v2-big"

    def test_mixed(self):
        assert normalize_name("runner.v2_big") == "runner-v2-big"


class TestValidateRunnerName:
    def test_valid_name(self):
        validate_runner_name("l-x86iavx512-8-16")

    def test_valid_with_dots(self):
        validate_runner_name("runner.v2")

    def test_empty_exits(self):
        with pytest.raises(SystemExit, match="1"):
            validate_runner_name("")

    def test_uppercase_exits(self):
        with pytest.raises(SystemExit, match="1"):
            validate_runner_name("UPPERCASE")

    def test_spaces_exits(self):
        with pytest.raises(SystemExit, match="1"):
            validate_runner_name("runner name")

    def test_special_chars_exits(self):
        with pytest.raises(SystemExit, match="1"):
            validate_runner_name("runner;echo")

    def test_leading_dash_exits(self):
        with pytest.raises(SystemExit, match="1"):
            validate_runner_name("-runner")


class TestConfigmapName:
    def test_basic(self):
        assert configmap_name("l-x86iavx512-8-16") == "capacity-config-l-x86iavx512-8-16"

    def test_normalized(self):
        assert configmap_name("runner.v2") == "capacity-config-runner-v2"

    def test_prefix(self):
        assert configmap_name("foo").startswith(CONFIGMAP_PREFIX)


class TestKubectlEnv:
    def test_appends_eks_to_no_proxy(self):
        env = kubectl_env()
        assert ".eks.amazonaws.com" in env["NO_PROXY"]
        assert ".eks.amazonaws.com" in env["no_proxy"]

    @patch.dict("os.environ", {"NO_PROXY": "localhost", "no_proxy": "localhost"})
    def test_preserves_existing_no_proxy(self):
        env = kubectl_env()
        assert env["NO_PROXY"] == "localhost,.eks.amazonaws.com"
        assert env["no_proxy"] == "localhost,.eks.amazonaws.com"


class TestRunKubectl:
    @patch("capacity_setter.subprocess.run")
    def test_runs_kubectl_with_args(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        result = run_kubectl(["get", "pods"])
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "kubectl"
        assert cmd[1:] == ["get", "pods"]
        assert result.returncode == 0

    @patch("capacity_setter.subprocess.run")
    def test_passes_env(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        run_kubectl(["version"])
        env = mock_run.call_args[1]["env"]
        assert ".eks.amazonaws.com" in env["NO_PROXY"]

    @patch("capacity_setter.subprocess.run")
    def test_passes_context(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        run_kubectl(["get", "pods"], context="my-cluster")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "kubectl"
        assert cmd[1:3] == ["--context", "my-cluster"]
        assert cmd[3:] == ["get", "pods"]

    @patch("capacity_setter.subprocess.run")
    def test_no_context_when_none(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        run_kubectl(["get", "pods"])
        cmd = mock_run.call_args[0][0]
        assert "--context" not in cmd

    @patch("capacity_setter.subprocess.run")
    def test_passes_input_data(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        run_kubectl(["apply", "-f", "-"], input_data='{"kind":"ConfigMap"}')
        assert mock_run.call_args[1]["input"] == '{"kind":"ConfigMap"}'


class TestEnsureKubeconfig:
    @patch("capacity_setter.subprocess.run")
    def test_calls_aws_eks(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ensure_kubeconfig("pytorch-arc-staging", "us-west-1")
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["aws", "eks", "update-kubeconfig"]
        assert "--name" in cmd
        assert "pytorch-arc-staging" in cmd
        assert "--region" in cmd
        assert "us-west-1" in cmd


class TestCmdSet:
    @patch("capacity_setter.run_kubectl")
    def test_applies_manifest(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0)
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", value=5, context=CTX)
        cmd_set(args)

        mock_kubectl.assert_called_once()
        call_args = mock_kubectl.call_args
        assert call_args[0][0] == ["apply", "-f", "-"]
        assert call_args[1]["context"] == CTX
        manifest = call_args[1]["input_data"]
        assert '"maxRunners": "5"' in manifest
        assert "capacity-config-l-x86iavx512-8-16" in manifest
        assert "capacity-config" in manifest

    @patch("capacity_setter.run_kubectl")
    def test_manifest_includes_labels(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0)
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", value=10, context=CTX)
        cmd_set(args)

        import json

        manifest = json.loads(mock_kubectl.call_args[1]["input_data"])
        labels = manifest["metadata"]["labels"]
        assert labels["app.kubernetes.io/component"] == "capacity-config"
        assert labels["osdc.io/module"] == "arc-runners"
        assert labels["osdc.io/runner-name"] == "l-x86iavx512-8-16"

    @patch("capacity_setter.run_kubectl")
    def test_manifest_namespace(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0)
        args = argparse.Namespace(runner_name="r1", value=0, context=CTX)
        cmd_set(args)

        import json

        manifest = json.loads(mock_kubectl.call_args[1]["input_data"])
        assert manifest["metadata"]["namespace"] == "arc-systems"


class TestCmdGet:
    @patch("capacity_setter.run_kubectl")
    def test_prints_value(self, mock_kubectl, capsys):
        mock_kubectl.return_value = MagicMock(returncode=0, stdout="5")
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        cmd_get(args)
        assert capsys.readouterr().out.strip() == "5"

    @patch("capacity_setter.run_kubectl")
    def test_passes_context(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0, stdout="5")
        args = argparse.Namespace(runner_name="r1", context=CTX)
        cmd_get(args)
        assert mock_kubectl.call_args[1]["context"] == CTX

    @patch("capacity_setter.run_kubectl")
    def test_exits_on_not_found(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=1)
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        with pytest.raises(SystemExit, match="1"):
            cmd_get(args)

    @patch("capacity_setter.run_kubectl")
    def test_exits_on_empty_value(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0, stdout="")
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        with pytest.raises(SystemExit, match="1"):
            cmd_get(args)


class TestCmdWatch:
    @patch("capacity_setter.subprocess.run")
    def test_calls_kubectl_watch(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        cmd_watch(args)
        cmd = mock_run.call_args[0][0]
        assert "-w" in cmd
        assert "configmap" in cmd
        assert "jsonpath" not in " ".join(cmd)

    @patch("capacity_setter.subprocess.run")
    def test_passes_context(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        args = argparse.Namespace(runner_name="r1", context=CTX)
        cmd_watch(args)
        cmd = mock_run.call_args[0][0]
        assert "--context" in cmd
        assert CTX in cmd

    @patch("capacity_setter.subprocess.run", side_effect=KeyboardInterrupt)
    def test_handles_keyboard_interrupt(self, mock_run, capsys):
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        cmd_watch(args)
        output = capsys.readouterr().out
        assert "\n" in output

    @patch(
        "capacity_setter.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "kubectl"),
    )
    def test_handles_called_process_error(self, mock_run):
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        with pytest.raises(SystemExit, match="1"):
            cmd_watch(args)


class TestCmdDelete:
    @patch("capacity_setter.run_kubectl")
    def test_deletes_existing(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0)
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        cmd_delete(args)
        delete_args = mock_kubectl.call_args[0][0]
        assert "delete" in delete_args
        assert "configmap" in delete_args

    @patch("capacity_setter.run_kubectl")
    def test_passes_context(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0)
        args = argparse.Namespace(runner_name="r1", context=CTX)
        cmd_delete(args)
        assert mock_kubectl.call_args[1]["context"] == CTX

    @patch("capacity_setter.run_kubectl")
    def test_warns_on_not_found(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=1)
        args = argparse.Namespace(runner_name="l-x86iavx512-8-16", context=CTX)
        cmd_delete(args)


class TestCmdList:
    @patch("capacity_setter.run_kubectl")
    def test_prints_output(self, mock_kubectl, capsys):
        mock_kubectl.return_value = MagicMock(
            returncode=0,
            stdout="RUNNER                  MAX_RUNNERS\nl-x86iavx512-8-16       5\n",
        )
        args = argparse.Namespace(context=CTX)
        cmd_list(args)
        out = capsys.readouterr().out
        assert "l-x86iavx512-8-16" in out
        assert "5" in out

    @patch("capacity_setter.run_kubectl")
    def test_passes_context(self, mock_kubectl):
        mock_kubectl.return_value = MagicMock(returncode=0, stdout="")
        args = argparse.Namespace(context=CTX)
        cmd_list(args)
        assert mock_kubectl.call_args[1]["context"] == CTX


class TestParseArgs:
    def test_set_command(self):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "set", "--runner-name", "r1", "--value", "5"]):
            args = parse_args()
            assert args.command == "set"
            assert args.runner_name == "r1"
            assert args.value == 5

    def test_get_command(self):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "get", "--runner-name", "r1"]):
            args = parse_args()
            assert args.command == "get"

    def test_list_command(self):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "list"]):
            args = parse_args()
            assert args.command == "list"

    def test_delete_command(self):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "delete", "--runner-name", "r1"]):
            args = parse_args()
            assert args.command == "delete"

    def test_watch_command(self):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "watch", "--runner-name", "r1"]):
            args = parse_args()
            assert args.command == "watch"

    def test_verbose_flag(self):
        with patch("sys.argv", ["prog", "--cluster-id", "x", "-v", "list"]):
            args = parse_args()
            assert args.verbose is True

    def test_missing_command_exits(self):
        with patch("sys.argv", ["prog", "--cluster-id", "x"]), pytest.raises(SystemExit):
            parse_args()


class TestMain:
    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.cmd_set")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_set_dispatches(self, mock_load, mock_cmd, mock_kube):
        with patch(
            "sys.argv",
            ["prog", "--cluster-id", "arc-staging", "set", "--runner-name", "r1", "--value", "5"],
        ):
            main()
            mock_kube.assert_called_once_with("pytorch-arc-staging", "us-west-1")
            mock_cmd.assert_called_once()
            passed_args = mock_cmd.call_args[0][0]
            assert passed_args.context == "pytorch-arc-staging"

    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.cmd_get")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_get_dispatches(self, mock_load, mock_cmd, mock_kube):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "get", "--runner-name", "r1"]):
            main()
            mock_cmd.assert_called_once()

    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.cmd_list")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_list_dispatches(self, mock_load, mock_cmd, mock_kube):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "list"]):
            main()
            mock_cmd.assert_called_once()

    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.cmd_delete")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_delete_dispatches(self, mock_load, mock_cmd, mock_kube):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "delete", "--runner-name", "r1"]):
            main()
            mock_cmd.assert_called_once()

    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.cmd_watch")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_watch_dispatches(self, mock_load, mock_cmd, mock_kube):
        with patch("sys.argv", ["prog", "--cluster-id", "arc-staging", "watch", "--runner-name", "r1"]):
            main()
            mock_cmd.assert_called_once()

    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_unknown_cluster_exits(self, mock_load):
        with (
            patch("sys.argv", ["prog", "--cluster-id", "nonexistent", "list"]),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.cmd_set")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_negative_value_exits(self, mock_load, mock_cmd, mock_kube):
        with (
            patch(
                "sys.argv",
                ["prog", "--cluster-id", "arc-staging", "set", "--runner-name", "r1", "--value", "-1"],
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            main()

    @patch("capacity_setter.ensure_kubeconfig")
    @patch("capacity_setter.load_clusters_yaml", return_value=FAKE_CLUSTERS)
    def test_invalid_runner_name_exits(self, mock_load, mock_kube):
        with (
            patch(
                "sys.argv",
                ["prog", "--cluster-id", "arc-staging", "get", "--runner-name", "INVALID NAME"],
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            main()


class TestLoadClustersYaml:
    def test_loads_valid_yaml(self, tmp_path):
        f = tmp_path / "clusters.yaml"
        f.write_text("clusters:\n  test:\n    cluster_name: test-cluster\n    region: us-east-1\n")
        result = load_clusters_yaml(f)
        assert result["clusters"]["test"]["cluster_name"] == "test-cluster"
