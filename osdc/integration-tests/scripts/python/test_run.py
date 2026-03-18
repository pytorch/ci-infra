"""Unit tests for the OSDC integration test orchestrator (run.py + phases.py)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from phases import (
    cleanup_stale_prs,
    ensure_canary_repo,
    generate_workflow,
    prepare_pr,
)
from run import (
    branch_name,
    format_duration,
    has_module,
    load_cluster_config,
    resolve,
)

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def clusters_yaml(tmp_path):
    """Write a minimal clusters.yaml and return its path."""
    content = {
        "defaults": {
            "harbor": {"core_replicas": 2},
            "monitoring": {"grafana_cloud_url": "https://default.example.com"},
            "logging": {"namespace": "logging"},
        },
        "clusters": {
            "arc-staging": {
                "cluster_name": "pytorch-arc-staging",
                "aws_region": "us-west-2",
                "modules": ["eks", "karpenter", "nodepools", "arc", "arc-runners"],
                "harbor": {"core_replicas": 1},
                "monitoring": {"grafana_cloud_url": "https://staging.example.com"},
            },
            "arc-production": {
                "cluster_name": "pytorch-arc-production",
                "aws_region": "us-east-2",
                "modules": ["eks", "karpenter", "nodepools", "arc", "arc-runners", "nodepools-b200", "arc-runners-b200"],
            },
        },
    }
    p = tmp_path / "clusters.yaml"
    import yaml

    p.write_text(yaml.dump(content))
    return p


@pytest.fixture
def cfg_staging(clusters_yaml):
    """Return loaded config for arc-staging."""
    return load_cluster_config(clusters_yaml, "arc-staging")


@pytest.fixture
def cfg_production(clusters_yaml):
    """Return loaded config for arc-production."""
    return load_cluster_config(clusters_yaml, "arc-production")


@pytest.fixture
def workflow_template(tmp_path):
    """Create a minimal workflow template and return the upstream dir."""
    upstream = tmp_path / "upstream"
    wf_dir = upstream / "integration-tests" / "workflows"
    wf_dir.mkdir(parents=True)

    template = (
        "name: {{PREFIX}} integration test\n"
        "on: push\n"
        "jobs:\n"
        "  basic:\n"
        "    runs-on: {{CLUSTER_NAME}}\n"
        "    steps:\n"
        "      - run: echo {{CLUSTER_ID}}\n"
        "  # BEGIN_B200\n"
        "  b200-job:\n"
        "    runs-on: b200-runner\n"
        "    steps:\n"
        "      - run: echo B200\n"
        "  # END_B200\n"
    )
    (wf_dir / "integration-test.yaml.tpl").write_text(template)

    # Also create build-image.yaml and Dockerfile for prepare_pr
    (wf_dir / "build-image.yaml").write_text("name: build-image\n")
    docker_dir = upstream / "integration-tests" / "docker" / "test-buildkit"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile").write_text("FROM alpine\n")

    return upstream


# ── load_cluster_config ───────────────────────────────────────────────────


class TestLoadClusterConfig:
    def test_valid_cluster(self, clusters_yaml):
        cfg = load_cluster_config(clusters_yaml, "arc-staging")
        assert cfg["cluster"]["cluster_name"] == "pytorch-arc-staging"
        assert cfg["cluster"]["aws_region"] == "us-west-2"
        assert "defaults" in cfg

    def test_missing_cluster(self, clusters_yaml):
        with pytest.raises(SystemExit):
            load_cluster_config(clusters_yaml, "nonexistent")


# ── resolve ───────────────────────────────────────────────────────────────


class TestResolve:
    def test_cluster_override(self, cfg_staging):
        # Cluster has core_replicas=1, defaults has 2
        assert resolve(cfg_staging, "harbor.core_replicas") == 1

    def test_defaults_fallback(self, cfg_staging):
        # logging.namespace only exists in defaults
        assert resolve(cfg_staging, "logging.namespace") == "logging"

    def test_nested_path(self, cfg_staging):
        assert resolve(cfg_staging, "monitoring.grafana_cloud_url") == "https://staging.example.com"

    def test_missing_with_default(self, cfg_staging):
        assert resolve(cfg_staging, "nonexistent.path", "fallback") == "fallback"

    def test_missing_without_default(self, cfg_staging):
        assert resolve(cfg_staging, "nonexistent.path") is None

    def test_top_level_key(self, cfg_staging):
        assert resolve(cfg_staging, "cluster_name") == "pytorch-arc-staging"


# ── has_module ────────────────────────────────────────────────────────────


class TestHasModule:
    def test_present(self, cfg_staging):
        assert has_module(cfg_staging, "karpenter") is True

    def test_absent(self, cfg_staging):
        assert has_module(cfg_staging, "nodepools-b200") is False

    def test_b200_present(self, cfg_production):
        assert has_module(cfg_production, "nodepools-b200") is True
        assert has_module(cfg_production, "arc-runners-b200") is True


# ── generate_workflow ─────────────────────────────────────────────────────


class TestGenerateWorkflow:
    def test_template_substitution(self, workflow_template):
        result = generate_workflow(
            workflow_template, "cbr", "arc-production", "pytorch-arc-production", b200_enabled=True,
        )
        assert "name: cbr integration test" in result
        assert "runs-on: pytorch-arc-production" in result
        assert "echo arc-production" in result

    def test_b200_removed_when_disabled(self, workflow_template):
        result = generate_workflow(
            workflow_template, "cbr", "arc-staging", "pytorch-arc-staging", b200_enabled=False,
        )
        assert "b200-job" not in result
        assert "BEGIN_B200" not in result
        assert "END_B200" not in result
        # The basic job should still be there
        assert "basic:" in result

    def test_b200_preserved_when_enabled(self, workflow_template):
        result = generate_workflow(
            workflow_template, "cbr", "arc-production", "pytorch-arc-production", b200_enabled=True,
        )
        assert "b200-job:" in result
        assert "echo B200" in result
        # Marker comments should be stripped
        assert "BEGIN_B200" not in result
        assert "END_B200" not in result


# ── format_duration ───────────────────────────────────────────────────────


class TestFormatDuration:
    def test_seconds_only(self):
        assert format_duration(45) == "45s"
        assert format_duration(0) == "0s"
        assert format_duration(59.9) == "1m00s"

    def test_minutes_and_seconds(self):
        assert format_duration(60) == "1m00s"
        assert format_duration(90) == "1m30s"
        assert format_duration(3661) == "61m01s"

    def test_fractional_seconds(self):
        assert format_duration(5.7) == "6s"


# ── cleanup_stale_prs ────────────────────────────────────────────────────


class TestCleanupStalePrs:
    @patch("phases.run_cmd")
    def test_closes_matching_prs_and_cancels_runs(self, mock_run):
        pr_list_stdout = json.dumps([
            {"number": 10, "title": "[NO REVIEW][NO MERGE] ARC smoke tests 2026-03-18"},
            {"number": 11, "title": "Unrelated PR"},
        ])
        queued_runs = json.dumps([{"databaseId": 100}])
        in_progress_runs = json.dumps([{"databaseId": 200}])

        mock_run.side_effect = [
            # 1: gh pr list
            MagicMock(returncode=0, stdout=pr_list_stdout),
            # 2: gh pr close #10 (only the matching PR)
            MagicMock(returncode=0),
            # 3: gh run list --status queued
            MagicMock(returncode=0, stdout=queued_runs),
            # 4: gh run cancel 100
            MagicMock(returncode=0),
            # 5: gh run list --status in_progress
            MagicMock(returncode=0, stdout=in_progress_runs),
            # 6: gh run cancel 200
            MagicMock(returncode=0),
        ]

        cleanup_stale_prs("osdc-integration-test-arc-staging")

        assert mock_run.call_count == 6

        # Verify PR #10 was closed
        close_call = mock_run.call_args_list[1]
        assert "10" in close_call[0][0]
        assert "close" in close_call[0][0]

        # Verify run 100 was cancelled
        cancel_call_1 = mock_run.call_args_list[3]
        assert "100" in cancel_call_1[0][0]

        # Verify run 200 was cancelled
        cancel_call_2 = mock_run.call_args_list[5]
        assert "200" in cancel_call_2[0][0]

    @patch("phases.run_cmd")
    def test_handles_pr_list_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="auth required", stdout="")
        cleanup_stale_prs("osdc-integration-test-arc-staging")
        # Should not raise, just log and return
        assert mock_run.call_count == 1

    @patch("phases.run_cmd")
    def test_no_matching_prs(self, mock_run):
        pr_list = json.dumps([{"number": 99, "title": "Something else"}])
        empty_runs = json.dumps([])

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=pr_list),       # pr list
            MagicMock(returncode=0, stdout=empty_runs),    # queued runs
            MagicMock(returncode=0, stdout=empty_runs),    # in_progress runs
        ]

        cleanup_stale_prs("osdc-integration-test-arc-staging")
        # pr list + 2 run list calls, no close/cancel calls
        assert mock_run.call_count == 3


# ── prepare_pr ────────────────────────────────────────────────────────────


class TestPreparePr:
    @patch("phases.run_cmd")
    def test_dry_run_no_push(self, mock_run, workflow_template, tmp_path):
        canary = tmp_path / "canary"
        canary.mkdir()

        # Simulate: git fetch, git checkout, git add, git diff --cached (has changes), git commit
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git checkout
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff --cached --quiet → changes exist
            MagicMock(returncode=0),  # git commit
        ]

        result = prepare_pr(
            canary_path=canary,
            upstream_dir=workflow_template,
            workflow_content="name: test\n",
            branch="osdc-integration-test-arc-staging",
            dry_run=True,
        )

        assert result is None  # No PR number in dry run

        # git push should NOT have been called
        for c in mock_run.call_args_list:
            cmd_list = c[0][0]
            assert "push" not in cmd_list

    @patch("phases.run_cmd")
    def test_writes_workflow_files(self, mock_run, workflow_template, tmp_path):
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git checkout
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff --cached --quiet → changes
            MagicMock(returncode=0),  # git commit
        ]

        prepare_pr(
            canary_path=canary,
            upstream_dir=workflow_template,
            workflow_content="name: test workflow\n",
            branch="osdc-integration-test-arc-staging",
            dry_run=True,
        )

        # Verify files were written
        wf_file = canary / ".github" / "workflows" / "integration-test.yaml"
        assert wf_file.exists()
        assert wf_file.read_text() == "name: test workflow\n"

        build_wf = canary / ".github" / "workflows" / "build-image.yaml"
        assert build_wf.exists()

        dockerfile = canary / "docker" / "test-buildkit" / "Dockerfile"
        assert dockerfile.exists()
        assert dockerfile.read_text() == "FROM alpine\n"


# ── ensure_canary_repo ───────────────────────────────────────────────────


class TestEnsureCanaryRepo:
    @patch("phases.run_cmd")
    def test_clones_when_missing(self, mock_run, tmp_path):
        upstream = tmp_path / "upstream"
        upstream.mkdir()

        mock_run.return_value = MagicMock(returncode=0)

        result = ensure_canary_repo(upstream)

        assert result == upstream / ".scratch" / "pytorch-canary"
        assert (upstream / ".scratch").is_dir()

        # Should have called gh repo clone
        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "clone" in cmd
        assert "pytorch/pytorch-canary" in cmd

    @patch("phases.run_cmd")
    def test_fetches_when_exists(self, mock_run, tmp_path):
        upstream = tmp_path / "upstream"
        canary = upstream / ".scratch" / "pytorch-canary"
        canary.mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0)

        result = ensure_canary_repo(upstream)

        assert result == canary

        # Should have called git fetch, not clone
        fetch_call = mock_run.call_args_list[0]
        cmd = fetch_call[0][0]
        assert "fetch" in cmd
        assert "clone" not in cmd


# ── branch_name ──────────────────────────────────────────────────────────


def test_branch_name():
    assert branch_name("arc-staging") == "osdc-integration-test-arc-staging"
    assert branch_name("arc-cbr-production") == "osdc-integration-test-arc-cbr-production"
