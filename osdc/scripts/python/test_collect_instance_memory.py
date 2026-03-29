"""Tests for collect_instance_memory module."""

import json
from unittest.mock import MagicMock, patch

import pytest
from collect_instance_memory import collect_node_memory, ki_to_mib, main
from instance_specs import INSTANCE_SPECS

# ---------------------------------------------------------------------------
# ki_to_mib
# ---------------------------------------------------------------------------


class TestKiToMib:
    def test_ki_suffix(self):
        assert ki_to_mib("16384Ki") == 16

    def test_ki_large_value(self):
        # 32505856Ki = ~31744 MiB
        assert ki_to_mib("32505856Ki") == 31744

    def test_plain_bytes(self):
        # 134217728 bytes = 128 MiB
        assert ki_to_mib("134217728") == 128

    def test_whitespace_stripped(self):
        assert ki_to_mib("  16384Ki  ") == 16

    def test_zero(self):
        assert ki_to_mib("0Ki") == 0


# ---------------------------------------------------------------------------
# collect_node_memory
# ---------------------------------------------------------------------------


SAMPLE_KUBECTL_OUTPUT = {
    "items": [
        {
            "metadata": {
                "labels": {
                    "node.kubernetes.io/instance-type": "c7a.48xlarge",
                }
            },
            "status": {
                "capacity": {
                    "memory": "393216Ki",
                }
            },
        },
        {
            "metadata": {
                "labels": {
                    "node.kubernetes.io/instance-type": "c7a.48xlarge",
                }
            },
            "status": {
                "capacity": {
                    "memory": "393216Ki",
                }
            },
        },
        {
            "metadata": {
                "labels": {
                    "node.kubernetes.io/instance-type": "g5.8xlarge",
                }
            },
            "status": {
                "capacity": {
                    "memory": "131072Ki",
                }
            },
        },
    ],
}


class TestCollectNodeMemory:
    @patch("collect_instance_memory.subprocess.run")
    def test_groups_by_instance_type(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(SAMPLE_KUBECTL_OUTPUT),
        )
        result = collect_node_memory(None)
        assert "c7a.48xlarge" in result
        assert len(result["c7a.48xlarge"]) == 2
        assert "g5.8xlarge" in result
        assert len(result["g5.8xlarge"]) == 1

    @patch("collect_instance_memory.subprocess.run")
    def test_kubectl_failure_exits(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="connection refused",
        )
        with pytest.raises(SystemExit):
            collect_node_memory(None)

    @patch("collect_instance_memory.subprocess.run")
    def test_kubeconfig_passed_to_env(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"items": []}),
        )
        collect_node_memory("/path/to/kubeconfig")
        call_env = mock_run.call_args[1]["env"]
        assert call_env["KUBECONFIG"] == "/path/to/kubeconfig"

    @patch("collect_instance_memory.subprocess.run")
    def test_skips_nodes_without_instance_type(self, mock_run):
        data = {
            "items": [
                {
                    "metadata": {"labels": {}},
                    "status": {"capacity": {"memory": "16384Ki"}},
                },
            ],
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(data))
        result = collect_node_memory(None)
        assert result == {}

    @patch("collect_instance_memory.subprocess.run")
    def test_skips_nodes_without_memory(self, mock_run):
        data = {
            "items": [
                {
                    "metadata": {"labels": {"node.kubernetes.io/instance-type": "c7a.48xlarge"}},
                    "status": {"capacity": {}},
                },
            ],
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(data))
        result = collect_node_memory(None)
        assert result == {}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @patch("collect_instance_memory.collect_node_memory")
    def test_main_with_cluster_data(self, mock_collect, capsys):
        mock_collect.return_value = {
            "c7a.48xlarge": [363724],
        }
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "c7a.48xlarge" in output

    @patch("collect_instance_memory.collect_node_memory")
    def test_main_unknown_instance_types(self, mock_collect, capsys):
        mock_collect.return_value = {
            "z99.nonexistent": [99999],
        }
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "NOT in INSTANCE_SPECS" in output
        assert "z99.nonexistent" in output

    @patch("collect_instance_memory.collect_node_memory")
    def test_main_no_cluster_data(self, mock_collect, capsys):
        mock_collect.return_value = {}
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "NOT PRESENT" in output

    @patch("collect_instance_memory.collect_node_memory")
    def test_main_kubeconfig_arg(self, mock_collect):
        mock_collect.return_value = {}
        result = main(["--kubeconfig", "/tmp/kube.conf"])
        assert result == 0
        mock_collect.assert_called_once_with("/tmp/kube.conf")

    @patch("collect_instance_memory.collect_node_memory")
    def test_main_actual_match(self, mock_collect, capsys):
        """When cluster value matches INSTANCE_SPECS, show YES."""
        spec_mi = INSTANCE_SPECS["c7a.48xlarge"]["memory_mi"]
        mock_collect.return_value = {"c7a.48xlarge": [spec_mi]}
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "YES" in output

    @patch("collect_instance_memory.collect_node_memory")
    def test_main_mismatch(self, mock_collect, capsys):
        """When cluster value differs from INSTANCE_SPECS, show NO."""
        mock_collect.return_value = {"c7a.48xlarge": [999999]}
        result = main([])
        assert result == 0
        output = capsys.readouterr().out
        assert "NO" in output
