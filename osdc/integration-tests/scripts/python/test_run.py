"""Unit tests for the OSDC integration test orchestrator (run.py + phases.py)."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from phases import (
    cleanup_stale_prs,
    clear_staging_pools,
    ensure_canary_repo,
    generate_workflow,
    prepare_pr,
)
from run import (
    branch_name,
    format_duration,
    gh_api,
    has_module,
    load_cluster_config,
    parse_args,
    resolve,
    run_cmd_with_retry,
    safe_json_loads,
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
                "modules": [
                    "eks",
                    "karpenter",
                    "nodepools",
                    "arc",
                    "arc-runners",
                    "nodepools-b200",
                    "arc-runners-b200",
                ],
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
        "    runs-on: {{PREFIX}}b200-runner\n"
        "    steps:\n"
        "      - run: echo B200\n"
        "  # END_B200\n"
        "  # BEGIN_CACHE_ENFORCER\n"
        "  cache-enforcer-job:\n"
        "    runs-on: {{PREFIX}}enforcer-runner\n"
        "    steps:\n"
        "      - run: echo enforcer\n"
        "  # END_CACHE_ENFORCER\n"
        "  pypi-job:\n"
        "    steps:\n"
        "      - run: echo {{PYPI_CACHE_SLUGS}}\n"
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
            workflow_template,
            "cbr",
            "arc-production",
            "pytorch-arc-production",
            b200_enabled=True,
        )
        assert "name: cbr integration test" in result
        assert "runs-on: pytorch-arc-production" in result
        assert "echo arc-production" in result

    def test_b200_removed_when_disabled(self, workflow_template):
        result = generate_workflow(
            workflow_template,
            "cbr",
            "arc-staging",
            "pytorch-arc-staging",
            b200_enabled=False,
        )
        assert "b200-job" not in result
        assert "BEGIN_B200" not in result
        assert "END_B200" not in result
        # The basic job should still be there
        assert "basic:" in result

    def test_b200_preserved_when_enabled(self, workflow_template):
        result = generate_workflow(
            workflow_template,
            "cbr",
            "arc-production",
            "pytorch-arc-production",
            b200_enabled=True,
        )
        assert "b200-job:" in result
        assert "echo B200" in result
        # Prefix must be substituted inside B200 block
        assert "runs-on: cbrb200-runner" in result
        assert "{{PREFIX}}" not in result
        # Marker comments should be stripped
        assert "BEGIN_B200" not in result
        assert "END_B200" not in result

    def test_cache_enforcer_removed_when_disabled(self, workflow_template):
        result = generate_workflow(
            workflow_template,
            "cbr",
            "arc-staging",
            "pytorch-arc-staging",
            b200_enabled=False,
            cache_enforcer_enabled=False,
        )
        assert "cache-enforcer-job" not in result
        assert "BEGIN_CACHE_ENFORCER" not in result
        assert "END_CACHE_ENFORCER" not in result
        assert "basic:" in result

    def test_cache_enforcer_preserved_when_enabled(self, workflow_template):
        result = generate_workflow(
            workflow_template,
            "cbr",
            "arc-staging",
            "pytorch-arc-staging",
            b200_enabled=False,
            cache_enforcer_enabled=True,
        )
        assert "cache-enforcer-job:" in result
        assert "echo enforcer" in result
        assert "runs-on: cbrenforcer-runner" in result
        assert "BEGIN_CACHE_ENFORCER" not in result
        assert "END_CACHE_ENFORCER" not in result

    def test_pypi_cache_slugs_substituted(self, workflow_template):
        result = generate_workflow(
            workflow_template,
            "cbr",
            "arc-staging",
            "pytorch-arc-staging",
            b200_enabled=False,
            pypi_cache_slugs="cpu cu121 cu124",
        )
        assert "echo cpu cu121 cu124" in result
        assert "{{PYPI_CACHE_SLUGS}}" not in result

    def test_pypi_cache_slugs_default(self, workflow_template):
        result = generate_workflow(
            workflow_template,
            "cbr",
            "arc-staging",
            "pytorch-arc-staging",
            b200_enabled=False,
        )
        # Default value should be substituted
        assert "echo cpu cu121 cu124" in result


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
        pr_list_stdout = json.dumps(
            [
                {"number": 10, "title": "[NO REVIEW][NO MERGE] ARC smoke tests 2026-03-18"},
                {"number": 11, "title": "Unrelated PR"},
            ]
        )
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
            MagicMock(returncode=0, stdout=pr_list),  # pr list
            MagicMock(returncode=0, stdout=empty_runs),  # queued runs
            MagicMock(returncode=0, stdout=empty_runs),  # in_progress runs
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

    @patch("phases.run_cmd")
    def test_pr_url_parse_failure(self, mock_run, workflow_template, tmp_path):
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git checkout
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff --cached --quiet → changes
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push
            MagicMock(returncode=0, stdout="not-a-url\n", stderr=""),  # gh pr create → garbage
        ]

        result = prepare_pr(
            canary_path=canary,
            upstream_dir=workflow_template,
            workflow_content="name: test\n",
            branch="osdc-integration-test-arc-staging",
            dry_run=False,
        )

        assert result is None


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

        # Call order: gh auth setup-git, gh repo clone, git config user.name, git config user.email
        assert mock_run.call_count == 4

        setup_call = mock_run.call_args_list[0]
        assert setup_call[0][0] == ["gh", "auth", "setup-git"]

        clone_call = mock_run.call_args_list[1]
        cmd = clone_call[0][0]
        assert "clone" in cmd
        assert "pytorch/pytorch-canary" in cmd

        # git config calls after clone
        name_call = mock_run.call_args_list[2]
        assert name_call[0][0] == ["git", "config", "user.name", "OSDC Integration Test"]
        email_call = mock_run.call_args_list[3]
        assert email_call[0][0] == ["git", "config", "user.email", "osdc-integration-test@pytorch.org"]

    @patch("phases.run_cmd")
    def test_fetches_when_exists(self, mock_run, tmp_path):
        upstream = tmp_path / "upstream"
        canary = upstream / ".scratch" / "pytorch-canary"
        canary.mkdir(parents=True)

        mock_run.return_value = MagicMock(returncode=0)

        result = ensure_canary_repo(upstream)

        assert result == canary

        # Call order: gh auth, rev-parse, fetch, git config x2
        assert mock_run.call_count == 5

        setup_call = mock_run.call_args_list[0]
        assert setup_call[0][0] == ["gh", "auth", "setup-git"]

        # rev-parse to verify integrity
        revparse_call = mock_run.call_args_list[1]
        assert revparse_call[0][0] == ["git", "rev-parse", "--git-dir"]

        # fetch
        fetch_call = mock_run.call_args_list[2]
        cmd = fetch_call[0][0]
        assert "fetch" in cmd
        assert "clone" not in cmd

        # git config calls after fetch
        name_call = mock_run.call_args_list[3]
        assert name_call[0][0] == ["git", "config", "user.name", "OSDC Integration Test"]

    @patch("phases.run_cmd")
    def test_reclones_when_corrupt(self, mock_run, tmp_path):
        upstream = tmp_path / "upstream"
        canary = upstream / ".scratch" / "pytorch-canary"
        canary.mkdir(parents=True)

        mock_run.side_effect = [
            MagicMock(returncode=0),  # gh auth setup-git
            MagicMock(returncode=128),  # rev-parse fails (corrupt)
            MagicMock(returncode=0),  # gh repo clone
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
        ]

        # shutil.rmtree will actually remove the directory (not mocked),
        # so the "if not canary_path.exists()" branch triggers the clone.
        result = ensure_canary_repo(upstream)

        assert result == canary

        # After real rmtree, should clone fresh
        clone_call = mock_run.call_args_list[2]
        cmd = clone_call[0][0]
        assert "clone" in cmd

    @patch("phases.run_cmd")
    def test_removes_stale_lock(self, mock_run, tmp_path):
        upstream = tmp_path / "upstream"
        canary = upstream / ".scratch" / "pytorch-canary"
        git_dir = canary / ".git"
        git_dir.mkdir(parents=True)
        lock_file = git_dir / "index.lock"
        lock_file.touch()

        mock_run.return_value = MagicMock(returncode=0)

        ensure_canary_repo(upstream)

        # Lock file should have been removed
        assert not lock_file.exists()

    @patch("phases.run_cmd")
    def test_auth_setup_failure_logs_warning(self, mock_run, tmp_path, caplog):
        upstream = tmp_path / "upstream"
        upstream.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="not logged in"),  # gh auth fails
            MagicMock(returncode=0),  # clone
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
        ]

        import logging

        with caplog.at_level(logging.WARNING):
            ensure_canary_repo(upstream)

        assert "gh auth setup-git failed" in caplog.text


# ── branch_name ──────────────────────────────────────────────────────────


def test_branch_name():
    assert branch_name("arc-staging") == "osdc-integration-test-arc-staging"
    assert branch_name("arc-cbr-production") == "osdc-integration-test-arc-cbr-production"


# ── run_cmd_with_retry ───────────────────────────────────────────────────


class TestRunCmdWithRetry:
    @patch("run.run_cmd")
    def test_succeeds_first_try(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        result = run_cmd_with_retry(["echo", "hi"], max_retries=3)
        assert result.returncode == 0
        assert mock_run.call_count == 1

    @patch("run.time.sleep")
    @patch("run.run_cmd")
    def test_retries_on_failure(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="err1"),
            MagicMock(returncode=1, stdout="", stderr="err2"),
            MagicMock(returncode=1, stdout="", stderr="err3"),
        ]
        result = run_cmd_with_retry(["gh", "api"], max_retries=3, base_delay=5.0)
        assert result.returncode == 1
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2  # no sleep after last attempt

    @patch("run.time.sleep")
    @patch("run.run_cmd")
    def test_exponential_backoff(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        result = run_cmd_with_retry(["cmd"], max_retries=3, base_delay=5.0)
        assert result.returncode == 0
        assert mock_sleep.call_count == 2
        # First delay: 5.0 * 2^0 = 5.0, second: 5.0 * 2^1 = 10.0
        mock_sleep.assert_any_call(5.0)
        mock_sleep.assert_any_call(10.0)


# ── safe_json_loads ──────────────────────────────────────────────────────


class TestSafeJsonLoads:
    def test_valid_json(self):
        assert safe_json_loads('{"key": "value"}') == {"key": "value"}
        assert safe_json_loads("[1, 2, 3]") == [1, 2, 3]

    def test_invalid_json(self):
        result = safe_json_loads("not json at all", context="test")
        assert result is None

    def test_empty_string(self):
        assert safe_json_loads("") is None
        assert safe_json_loads("   ") is None

    def test_none_input(self):
        assert safe_json_loads(None) is None


# ── gh_api ────────────────────────────────────────────────────────────────


class TestGhApi:
    @patch("run.run_cmd")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"id": 1}',
            stderr="",
        )
        result = gh_api("/repos/test")
        assert result == {"id": 1}
        cmd = mock_run.call_args[0][0]
        assert "gh" in cmd
        assert "api" in cmd
        assert "/repos/test" in cmd

    @patch("run.run_cmd")
    def test_with_method_and_fields(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )
        result = gh_api("/repos/test", method="POST", title="hello")
        assert result == {"ok": True}
        cmd = mock_run.call_args[0][0]
        assert "--method" in cmd
        assert "POST" in cmd
        assert "-f" in cmd
        assert "title=hello" in cmd

    @patch("run.run_cmd")
    def test_failure_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Not Found",
        )
        result = gh_api("/repos/nonexistent")
        assert result is None

    @patch("run.run_cmd")
    def test_invalid_json_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not-json",
            stderr="",
        )
        result = gh_api("/repos/test")
        assert result is None


# ── parse_args ────────────────────────────────────────────────────────────


class TestParseArgs:
    def test_required_args(self):
        with patch(
            "sys.argv",
            [
                "run.py",
                "--cluster-id",
                "arc-staging",
                "--clusters-yaml",
                "/tmp/c.yaml",
                "--upstream-dir",
                "/tmp/upstream",
                "--root-dir",
                "/tmp/root",
            ],
        ):
            args = parse_args()
        assert args.cluster_id == "arc-staging"
        assert str(args.clusters_yaml) == "/tmp/c.yaml"
        assert str(args.upstream_dir) == "/tmp/upstream"
        assert str(args.root_dir) == "/tmp/root"
        assert args.run_smoke is False
        assert args.run_compactor is False
        assert args.skip_smoke is False
        assert args.skip_compactor is False
        assert args.dry_run is False
        assert args.keep_pr is False
        assert args.force is False
        assert args.skip_drain is False

    def test_all_flags(self):
        with patch(
            "sys.argv",
            [
                "run.py",
                "--cluster-id",
                "prod",
                "--clusters-yaml",
                "/c.yaml",
                "--upstream-dir",
                "/u",
                "--root-dir",
                "/r",
                "--run-smoke",
                "--run-compactor",
                "--skip-smoke",
                "--skip-compactor",
                "--dry-run",
                "--keep-pr",
                "--force",
                "--skip-drain",
            ],
        ):
            args = parse_args()
        assert args.run_smoke is True
        assert args.run_compactor is True
        assert args.skip_smoke is True
        assert args.skip_compactor is True
        assert args.dry_run is True
        assert args.keep_pr is True
        assert args.force is True
        assert args.skip_drain is True

    def test_missing_required_exits(self):
        with patch("sys.argv", ["run.py"]), pytest.raises(SystemExit):
            parse_args()


# ── clear_staging_pools ──────────────────────────────────────────────────


class TestClearStagingPools:
    @patch("phases.run_cmd")
    def test_skips_non_staging(self, mock_run):
        """Should return immediately for non-staging clusters."""
        clear_staging_pools("arc-production")
        mock_run.assert_not_called()

    @patch("phases.run_cmd")
    def test_no_active_pods_skips(self, mock_run):
        """When no runner pods are found, skip pool clear."""
        mock_run.return_value = MagicMock(returncode=0, stdout="\n", stderr="")
        clear_staging_pools("arc-staging")
        # Only the initial kubectl get pods call
        assert mock_run.call_count == 1

    @patch("phases.run_cmd")
    def test_kubectl_failure_returns(self, mock_run):
        """If kubectl get pods fails, log warning and return."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="connection refused",
        )
        clear_staging_pools("arc-staging")
        assert mock_run.call_count == 1

    @patch("phases.time.sleep")
    @patch("phases.run_cmd")
    def test_force_drains_and_redeploys(self, mock_run, mock_sleep):
        """With force=True, delete pods, nodepools, wait for drain, redeploy."""
        mock_run.side_effect = [
            # kubectl get pods (has active pods)
            MagicMock(returncode=0, stdout="pod1 Running\npod2 Running\n", stderr=""),
            # kubectl delete pods
            MagicMock(returncode=0, stdout="", stderr=""),
            # kubectl delete nodepools
            MagicMock(returncode=0, stdout="", stderr=""),
            # kubectl get nodes (first check — nodes still exist)
            MagicMock(returncode=0, stdout="node1 Ready\n", stderr=""),
            # kubectl get nodes (second check — drained)
            MagicMock(returncode=0, stdout="", stderr=""),
            # just deploy-module
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        clear_staging_pools("arc-staging", force=True)

        assert mock_run.call_count == 6
        # Verify delete pods call
        delete_pods = mock_run.call_args_list[1][0][0]
        assert "delete" in delete_pods
        assert "pods" in delete_pods
        # Verify delete nodepools call
        delete_np = mock_run.call_args_list[2][0][0]
        assert "delete" in delete_np
        assert "nodepools" in delete_np
        # Verify redeploy call
        deploy_call = mock_run.call_args_list[5][0][0]
        assert "deploy-module" in deploy_call
        # Sleep called once while waiting for drain
        assert mock_sleep.call_count == 1
        mock_sleep.assert_called_with(10)

    @patch("builtins.input", return_value="n")
    @patch("phases.run_cmd")
    def test_interactive_decline_skips(self, mock_run, mock_input):
        """Without force, declining the prompt skips pool clear."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="pod1 Running\n",
            stderr="",
        )
        clear_staging_pools("arc-staging", force=False)
        # Only the get-pods call, no delete/drain/redeploy
        assert mock_run.call_count == 1

    @patch("builtins.input", return_value="y")
    @patch("phases.time.sleep")
    @patch("phases.run_cmd")
    def test_interactive_accept_proceeds(self, mock_run, mock_sleep, mock_input):
        """Answering 'y' to the prompt proceeds with drain."""
        mock_run.side_effect = [
            # kubectl get pods (active)
            MagicMock(returncode=0, stdout="pod1 Running\n", stderr=""),
            # kubectl delete pods
            MagicMock(returncode=0, stdout="", stderr=""),
            # kubectl delete nodepools
            MagicMock(returncode=0, stdout="", stderr=""),
            # kubectl get nodes (empty = drained)
            MagicMock(returncode=0, stdout="", stderr=""),
            # just deploy-module
            MagicMock(returncode=0, stdout="", stderr=""),
        ]

        clear_staging_pools("arc-staging", force=False)

        assert mock_run.call_count == 5
        # Verify the drain and redeploy happened
        deploy_call = mock_run.call_args_list[4][0][0]
        assert "deploy-module" in deploy_call


# ── prepare_pr (additional edge cases) ────────────────────────────────────


class TestPreparePrAdditional:
    @patch("phases.run_cmd")
    def test_no_changes_skips_commit(self, mock_run, workflow_template, tmp_path):
        """When git diff --cached --quiet returns 0, skip commit."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git checkout
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0),  # git diff --cached --quiet → no changes
            # No commit call expected; next is dry-run exit
        ]

        result = prepare_pr(
            canary_path=canary,
            upstream_dir=workflow_template,
            workflow_content="name: test\n",
            branch="osdc-integration-test-arc-staging",
            dry_run=True,
        )

        assert result is None
        # Only 4 calls: fetch, checkout, add, diff (no commit)
        assert mock_run.call_count == 4

    @patch("phases.run_cmd")
    def test_existing_workflows_dir_removed(self, mock_run, workflow_template, tmp_path):
        """When .github/workflows already exists, it gets removed before writing."""
        canary = tmp_path / "canary"
        canary.mkdir()
        old_wf_dir = canary / ".github" / "workflows"
        old_wf_dir.mkdir(parents=True)
        (old_wf_dir / "stale-workflow.yaml").write_text("old content")

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git checkout
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff --cached --quiet → changes exist
            MagicMock(returncode=0),  # git commit
        ]

        prepare_pr(
            canary_path=canary,
            upstream_dir=workflow_template,
            workflow_content="name: new test\n",
            branch="osdc-integration-test-arc-staging",
            dry_run=True,
        )

        # Stale file should be gone
        assert not (old_wf_dir / "stale-workflow.yaml").exists()
        # New workflow should be written
        assert (old_wf_dir / "integration-test.yaml").read_text() == "name: new test\n"

    @patch("phases.run_cmd")
    def test_successful_pr_returns_number(self, mock_run, workflow_template, tmp_path):
        """Full non-dry-run flow returns parsed PR number."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git fetch
            MagicMock(returncode=0),  # git checkout
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff --cached → changes
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push
            MagicMock(returncode=0, stdout="https://github.com/pytorch/pytorch-canary/pull/42\n", stderr=""),
        ]

        result = prepare_pr(
            canary_path=canary,
            upstream_dir=workflow_template,
            workflow_content="name: test\n",
            branch="osdc-integration-test-arc-staging",
            dry_run=False,
        )

        assert result == 42


# ── main() ───────────────────────────────────────────────────────────────


class TestMain:
    @patch("sys.exit")
    @patch("run.main")
    def test_main_dry_run(self, mock_main, mock_exit):
        """Verify main() is callable (import-level sanity check)."""
        # main() relies on imports of phases and phases_validation inside the
        # function body, so we just verify it's importable and callable.
        from run import main as main_fn

        assert callable(main_fn)

    @patch("run.parse_args")
    def test_main_called_process_error(self, mock_parse_args, clusters_yaml, tmp_path):
        """main() catches CalledProcessError and exits 1."""
        from run import main

        mock_parse_args.return_value = MagicMock(
            cluster_id="arc-staging",
            clusters_yaml=clusters_yaml,
            upstream_dir=tmp_path,
            root_dir=tmp_path,
            run_smoke=False,
            run_compactor=False,
            skip_smoke=True,
            skip_compactor=True,
            dry_run=False,
            keep_pr=False,
            force=True,
            skip_drain=True,
        )

        with (
            patch("phases.cleanup_stale_prs"),
            patch("phases.ensure_canary_repo"),
            patch("phases.generate_workflow", return_value="wf content"),
            patch(
                "phases.prepare_pr",
                side_effect=subprocess.CalledProcessError(
                    1,
                    ["git", "push"],
                    stderr="auth fail",
                ),
            ),
            patch("phases_validation.print_report"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1

    @patch("run.parse_args")
    def test_main_keyboard_interrupt(self, mock_parse_args, clusters_yaml, tmp_path):
        """main() catches KeyboardInterrupt, prints partial report, and exits."""
        from run import main

        mock_parse_args.return_value = MagicMock(
            cluster_id="arc-staging",
            clusters_yaml=clusters_yaml,
            upstream_dir=tmp_path,
            root_dir=tmp_path,
            run_smoke=False,
            run_compactor=False,
            skip_smoke=True,
            skip_compactor=True,
            dry_run=False,
            keep_pr=False,
            force=True,
            skip_drain=True,
        )

        with (
            patch("phases.cleanup_stale_prs"),
            patch("phases.ensure_canary_repo", return_value=tmp_path),
            patch("phases.generate_workflow", return_value="wf"),
            patch("phases.prepare_pr", return_value=99),
            patch("phases_validation.run_parallel_validation", side_effect=KeyboardInterrupt),
            patch("phases_validation._fetch_latest_runs", return_value=[]),
            patch("phases_validation._collect_run_details", return_value=[]),
            patch("phases_validation.print_report", return_value=False) as mock_report,
            patch("phases_validation.close_pr") as mock_close,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        mock_report.assert_called_once()
        # interrupted flag should be set
        assert mock_report.call_args[1].get("interrupted") is True or mock_report.call_args[0][-1] is True  # positional
        mock_close.assert_called_once()

    @patch("run.parse_args")
    def test_main_keep_pr_flag(self, mock_parse_args, clusters_yaml, tmp_path):
        """With --keep-pr, main() does not close the PR."""
        from run import main

        mock_parse_args.return_value = MagicMock(
            cluster_id="arc-staging",
            clusters_yaml=clusters_yaml,
            upstream_dir=tmp_path,
            root_dir=tmp_path,
            run_smoke=False,
            run_compactor=False,
            skip_smoke=True,
            skip_compactor=True,
            dry_run=False,
            keep_pr=True,
            force=True,
            skip_drain=True,
        )

        with (
            patch("phases.cleanup_stale_prs"),
            patch("phases.ensure_canary_repo", return_value=tmp_path),
            patch("phases.generate_workflow", return_value="wf"),
            patch("phases.prepare_pr", return_value=99),
            patch("phases_validation.run_parallel_validation", return_value={}),
            patch("phases_validation.wait_for_workflows", return_value=[]),
            patch("phases_validation.print_report", return_value=True),
            patch("phases_validation.close_pr") as mock_close,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        mock_close.assert_not_called()

    @patch("run.parse_args")
    def test_main_dry_run_exits_early(self, mock_parse_args, clusters_yaml, tmp_path):
        """With --dry-run, main() exits after prepare_pr without polling."""
        from run import main

        mock_parse_args.return_value = MagicMock(
            cluster_id="arc-staging",
            clusters_yaml=clusters_yaml,
            upstream_dir=tmp_path,
            root_dir=tmp_path,
            run_smoke=False,
            run_compactor=False,
            skip_smoke=True,
            skip_compactor=True,
            dry_run=True,
            keep_pr=False,
            force=True,
            skip_drain=False,
        )

        with (
            patch("phases.cleanup_stale_prs"),
            patch("phases.clear_staging_pools") as mock_clear,
            patch("phases.ensure_canary_repo", return_value=tmp_path),
            patch("phases.generate_workflow", return_value="wf"),
            patch("phases.prepare_pr", return_value=None),
            patch("phases_validation.run_parallel_validation") as mock_validation,
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 0
        # dry_run skips drain and validation
        mock_clear.assert_not_called()
        mock_validation.assert_not_called()
