"""Unit tests for resolve_pytorch_image."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from resolve_pytorch_image import resolve_ci_docker_hash


class TestResolveCiDockerHash:
    @patch("resolve_pytorch_image.subprocess.run")
    def test_returns_sha_on_happy_path(self, mock_run):
        mock_run.return_value = MagicMock(stdout=json.dumps({"sha": "abc123"}))
        assert resolve_ci_docker_hash() == "abc123"

        call = mock_run.call_args_list[0]
        assert call[0][0] == [
            "gh",
            "api",
            "repos/pytorch/pytorch/git/trees/main:.ci/docker",
        ]
        assert call[1]["check"] is True
        assert call[1]["capture_output"] is True
        assert call[1]["text"] is True

    @patch("resolve_pytorch_image.subprocess.run")
    def test_non_zero_exit_raises(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["gh"], stderr="boom"
        )
        with pytest.raises(RuntimeError, match=r"gh api .* failed"):
            resolve_ci_docker_hash()

    @patch("resolve_pytorch_image.subprocess.run")
    def test_invalid_json_raises(self, mock_run):
        mock_run.return_value = MagicMock(stdout="not json at all")
        with pytest.raises(RuntimeError, match="invalid JSON"):
            resolve_ci_docker_hash()

    @patch("resolve_pytorch_image.subprocess.run")
    def test_missing_sha_field_raises(self, mock_run):
        mock_run.return_value = MagicMock(stdout=json.dumps({"name": "no-sha-here"}))
        with pytest.raises(RuntimeError, match="missing 'sha' field"):
            resolve_ci_docker_hash()

    @patch("resolve_pytorch_image.subprocess.run")
    def test_non_dict_payload_raises(self, mock_run):
        mock_run.return_value = MagicMock(stdout=json.dumps([{"sha": "abc"}]))
        with pytest.raises(RuntimeError, match="missing 'sha' field"):
            resolve_ci_docker_hash()

    @patch("resolve_pytorch_image.subprocess.run")
    def test_custom_ref_in_url(self, mock_run):
        mock_run.return_value = MagicMock(stdout=json.dumps({"sha": "deadbeef"}))
        resolve_ci_docker_hash(ref="release/2.5")
        called_cmd = mock_run.call_args_list[0][0][0]
        assert called_cmd[2] == "repos/pytorch/pytorch/git/trees/release/2.5:.ci/docker"

    @patch("resolve_pytorch_image.subprocess.run")
    def test_missing_gh_binary_raises_runtime_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("gh")
        with pytest.raises(RuntimeError, match="gh CLI not found on PATH"):
            resolve_ci_docker_hash()

    @patch("resolve_pytorch_image.subprocess.run")
    def test_gh_timeout_raises_runtime_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gh"], timeout=30)
        with pytest.raises(RuntimeError, match=r"timed out after 30s"):
            resolve_ci_docker_hash()

    @patch("resolve_pytorch_image.subprocess.run")
    def test_gh_call_passes_timeout(self, mock_run):
        mock_run.return_value = MagicMock(stdout=json.dumps({"sha": "abc"}))
        resolve_ci_docker_hash()
        assert mock_run.call_args_list[0][1]["timeout"] == 30
