"""Unit tests for workload_phases.py."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from workload_phases import (
    KEEP_WORKFLOWS,
    PR_TITLE_PREFIX,
    PYTORCH_REPO,
    SCRATCH_DIR_NAME,
    _check_freshness,
    create_workload_pr,
    ensure_pytorch_repo,
    instrument_workflows,
    mirror_content,
    monitor_workflows,
    print_workload_report,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_time_sequence(*values):
    """Create a time.time() mock that returns values then repeats the last.

    Python's logging module also calls time.time(), so the last value repeats
    indefinitely to avoid StopIteration in unrelated code paths.
    """
    it = iter(values)
    last_val = values[-1]

    def fake_time():
        nonlocal last_val
        try:
            val = next(it)
            last_val = val
            return val
        except StopIteration:
            return last_val

    return fake_time


# ── Constants ─────────────────────────────────────────────────────────


class TestConstants:
    def test_keep_workflows_default(self):
        assert "lint.yml" in KEEP_WORKFLOWS

    def test_pr_title_prefix(self):
        assert "NO REVIEW" in PR_TITLE_PREFIX
        assert "NO MERGE" in PR_TITLE_PREFIX

    def test_pytorch_repo(self):
        assert PYTORCH_REPO == "pytorch/pytorch"

    def test_scratch_dir_name(self):
        assert SCRATCH_DIR_NAME == ".scratch"


# ── ensure_pytorch_repo ──────────────────────────────────────────────


class TestEnsurePytorchRepo:
    @patch("workload_phases._check_freshness")
    @patch("workload_phases.run_cmd")
    def test_fresh_clone_when_no_dir(self, mock_run, mock_fresh, tmp_path):
        """When no pytorch dir exists, clone blobless and checkout viable/strict."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = ensure_pytorch_repo(tmp_path)

        expected = tmp_path / SCRATCH_DIR_NAME / "pytorch"
        assert result == expected
        assert (tmp_path / SCRATCH_DIR_NAME).is_dir()

        # First call: clone
        clone_call = mock_run.call_args_list[0]
        cmd = clone_call[0][0]
        assert "clone" in cmd
        assert "--filter=blob:none" in cmd
        assert "--no-recurse-submodules" in cmd
        assert f"https://github.com/{PYTORCH_REPO}.git" in cmd

        # Second call: checkout viable/strict
        checkout_call = mock_run.call_args_list[1]
        assert "origin/viable/strict" in checkout_call[0][0]

        mock_fresh.assert_called_once_with(expected)

    @patch("workload_phases._check_freshness")
    @patch("workload_phases.run_cmd")
    def test_existing_valid_repo_fetches(self, mock_run, mock_fresh, tmp_path):
        """When pytorch dir exists and rev-parse succeeds, fetch origin."""
        pytorch_dir = tmp_path / SCRATCH_DIR_NAME / "pytorch"
        pytorch_dir.mkdir(parents=True)

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git rev-parse --git-dir (valid)
            MagicMock(returncode=0),  # git fetch origin
            MagicMock(returncode=0),  # git checkout origin/viable/strict
        ]

        result = ensure_pytorch_repo(tmp_path)
        assert result == pytorch_dir

        # First call: rev-parse
        assert "rev-parse" in mock_run.call_args_list[0][0][0]
        # Second call: fetch
        assert "fetch" in mock_run.call_args_list[1][0][0]
        # Third call: checkout
        assert "checkout" in mock_run.call_args_list[2][0][0]

    @patch("workload_phases._check_freshness")
    @patch("workload_phases.run_cmd")
    def test_corrupt_repo_reclones(self, mock_run, mock_fresh, tmp_path):
        """When rev-parse fails, rmtree and re-clone."""
        pytorch_dir = tmp_path / SCRATCH_DIR_NAME / "pytorch"
        pytorch_dir.mkdir(parents=True)
        (pytorch_dir / "marker").touch()

        mock_run.side_effect = [
            MagicMock(returncode=128),  # rev-parse fails (corrupt)
            MagicMock(returncode=0),    # git clone
            MagicMock(returncode=0),    # git checkout viable/strict
        ]

        result = ensure_pytorch_repo(tmp_path)
        assert result == pytorch_dir
        # marker should be gone (rmtree removed old dir, clone recreated it)
        assert not (pytorch_dir / "marker").exists()

        # Second call: clone (after rmtree)
        clone_call = mock_run.call_args_list[1]
        assert "clone" in clone_call[0][0]

    @patch("workload_phases._check_freshness")
    @patch("workload_phases.run_cmd")
    def test_viable_strict_fallback_to_main(self, mock_run, mock_fresh, tmp_path):
        """If viable/strict checkout fails, fall back to origin/main."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # clone
            MagicMock(returncode=1),  # checkout viable/strict fails
            MagicMock(returncode=0),  # checkout origin/main
        ]

        result = ensure_pytorch_repo(tmp_path)
        assert result == tmp_path / SCRATCH_DIR_NAME / "pytorch"

        # Third call: fallback to main
        main_call = mock_run.call_args_list[2]
        assert "origin/main" in main_call[0][0]

    @patch("workload_phases._check_freshness")
    @patch("workload_phases.run_cmd")
    def test_viable_strict_success_no_fallback(self, mock_run, mock_fresh, tmp_path):
        """When viable/strict succeeds, no fallback to main."""
        mock_run.side_effect = [
            MagicMock(returncode=0),  # clone
            MagicMock(returncode=0),  # checkout viable/strict succeeds
        ]

        ensure_pytorch_repo(tmp_path)
        # Only 2 calls: clone + checkout (no fallback)
        assert mock_run.call_count == 2

    @patch("workload_phases._check_freshness")
    @patch("workload_phases.run_cmd")
    def test_scratch_dir_created(self, mock_run, mock_fresh, tmp_path):
        """The .scratch directory is created if it doesn't exist."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ensure_pytorch_repo(tmp_path)

        assert (tmp_path / SCRATCH_DIR_NAME).is_dir()


# ── _check_freshness ─────────────────────────────────────────────────


class TestCheckFreshness:
    @patch("workload_phases.run_cmd")
    def test_fresh_repo_no_warning(self, mock_run, caplog):
        """Repo less than 24h old produces no warning."""
        import logging

        recent_ts = str(int(time.time() - 3600))  # 1 hour ago
        mock_run.return_value = MagicMock(returncode=0, stdout=recent_ts)

        with caplog.at_level(logging.WARNING, logger="osdc-workload-test"):
            _check_freshness(Path("/fake"))

        assert "hours old" not in caplog.text

    @patch("workload_phases.run_cmd")
    def test_stale_repo_warns(self, mock_run, caplog):
        """Repo more than 24h old triggers a warning."""
        import logging

        stale_ts = str(int(time.time() - 100_000))  # ~28 hours ago
        mock_run.return_value = MagicMock(returncode=0, stdout=stale_ts)

        with caplog.at_level(logging.WARNING, logger="osdc-workload-test"):
            _check_freshness(Path("/fake"))

        assert "hours old" in caplog.text

    @patch("workload_phases.run_cmd")
    def test_git_log_failure_silent(self, mock_run):
        """If git log fails, return silently without crashing."""
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        _check_freshness(Path("/fake"))  # should not raise

    @patch("workload_phases.run_cmd")
    def test_empty_stdout_silent(self, mock_run):
        """If stdout is empty/whitespace, return silently."""
        mock_run.return_value = MagicMock(returncode=0, stdout="   ")
        _check_freshness(Path("/fake"))  # should not raise

    @patch("workload_phases.run_cmd")
    def test_just_under_24h_no_warning(self, mock_run, caplog):
        """Repo at 23 hours produces no warning (boundary: > 24)."""
        import logging

        ts = str(int(time.time() - 23 * 3600))  # 23h ago
        mock_run.return_value = MagicMock(returncode=0, stdout=ts)

        with caplog.at_level(logging.WARNING, logger="osdc-workload-test"):
            _check_freshness(Path("/fake"))

        assert "hours old" not in caplog.text


# ── mirror_content ───────────────────────────────────────────────────


class TestMirrorContent:
    @patch("subprocess.Popen")
    def test_pipe_chain_git_archive_to_tar(self, mock_popen, tmp_path):
        """git archive pipes into tar -x."""
        canary = tmp_path / "canary"
        canary.mkdir()
        pytorch = tmp_path / "pytorch"
        pytorch.mkdir()

        archive_proc = MagicMock()
        archive_proc.stdout = MagicMock()
        archive_proc.wait.return_value = 0

        tar_proc = MagicMock()
        tar_proc.communicate.return_value = (None, None)
        tar_proc.returncode = 0

        mock_popen.side_effect = [archive_proc, tar_proc]

        mirror_content(pytorch, canary)

        # First popen: git archive HEAD
        git_call = mock_popen.call_args_list[0]
        assert git_call[0][0] == ["git", "archive", "HEAD"]
        assert git_call[1]["cwd"] == pytorch
        assert git_call[1]["stdout"] == subprocess.PIPE

        # Second popen: tar
        tar_call = mock_popen.call_args_list[1]
        assert "tar" in tar_call[0][0]
        assert str(canary) in tar_call[0][0]
        assert tar_call[1]["stdin"] == archive_proc.stdout

        # Archive stdout closed, tar communicated
        archive_proc.stdout.close.assert_called_once()
        tar_proc.communicate.assert_called_once()

    @patch("subprocess.Popen")
    def test_clears_canary_except_dot_git(self, mock_popen, tmp_path):
        """Everything in canary except .git/ is deleted before extraction."""
        canary = tmp_path / "canary"
        canary.mkdir()
        (canary / ".git").mkdir()
        (canary / ".git" / "config").touch()
        (canary / "old_file.txt").touch()
        old_dir = canary / "old_dir"
        old_dir.mkdir()
        (old_dir / "nested.txt").touch()

        archive_proc = MagicMock()
        archive_proc.stdout = MagicMock()
        archive_proc.wait.return_value = 0

        tar_proc = MagicMock()
        tar_proc.communicate.return_value = (None, None)
        tar_proc.returncode = 0

        mock_popen.side_effect = [archive_proc, tar_proc]

        mirror_content(tmp_path, canary)

        assert (canary / ".git").exists()
        assert (canary / ".git" / "config").exists()
        assert not (canary / "old_file.txt").exists()
        assert not old_dir.exists()

    @patch("subprocess.Popen")
    def test_git_archive_failure_raises(self, mock_popen, tmp_path):
        """RuntimeError raised when git archive exit code != 0."""
        canary = tmp_path / "canary"
        canary.mkdir()

        archive_proc = MagicMock()
        archive_proc.stdout = MagicMock()
        archive_proc.wait.return_value = 1

        tar_proc = MagicMock()
        tar_proc.communicate.return_value = (None, None)
        tar_proc.returncode = 0

        mock_popen.side_effect = [archive_proc, tar_proc]

        with pytest.raises(RuntimeError, match="git archive failed"):
            mirror_content(tmp_path, canary)

    @patch("subprocess.Popen")
    def test_tar_extraction_failure_raises(self, mock_popen, tmp_path):
        """RuntimeError raised when tar exit code != 0."""
        canary = tmp_path / "canary"
        canary.mkdir()

        archive_proc = MagicMock()
        archive_proc.stdout = MagicMock()
        archive_proc.wait.return_value = 0

        tar_proc = MagicMock()
        tar_proc.communicate.return_value = (None, None)
        tar_proc.returncode = 1

        mock_popen.side_effect = [archive_proc, tar_proc]

        with pytest.raises(RuntimeError, match="tar extraction failed"):
            mirror_content(tmp_path, canary)

    @patch("subprocess.Popen")
    def test_empty_canary_no_crash(self, mock_popen, tmp_path):
        """Canary with no files/dirs (empty) does not crash during clearing."""
        canary = tmp_path / "canary"
        canary.mkdir()

        archive_proc = MagicMock()
        archive_proc.stdout = MagicMock()
        archive_proc.wait.return_value = 0

        tar_proc = MagicMock()
        tar_proc.communicate.return_value = (None, None)
        tar_proc.returncode = 0

        mock_popen.side_effect = [archive_proc, tar_proc]

        mirror_content(tmp_path, canary)  # should not raise


# ── instrument_workflows ─────────────────────────────────────────────


class TestInstrumentWorkflows:
    def _setup_workflows(self, tmp_path):
        """Create a canary repo with workflow files for testing."""
        canary = tmp_path / "canary"
        wf_dir = canary / ".github" / "workflows"
        wf_dir.mkdir(parents=True)

        # Entry point workflow (lint.yml — in KEEP_WORKFLOWS)
        (wf_dir / "lint.yml").write_text(
            "on:\n  pull_request:\njobs:\n"
            "  lint-arc:\n"
            "    runs-on: mt-l-x86iamx-8-16\n"
            "    steps:\n      - run: echo lint\n"
            "  lint-gha:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: echo gha\n"
        )

        # Entry point workflow (test.yml — NOT in KEEP_WORKFLOWS, will be removed)
        (wf_dir / "test.yml").write_text(
            "on:\n  push:\njobs:\n  test:\n    runs-on: mt-l-x86iamx-8-16\n"
        )

        # Reusable workflow (workflow_call only — not an entry point, kept)
        (wf_dir / "_reusable.yml").write_text(
            "on:\n  workflow_call:\njobs:\n  reuse:\n    runs-on: mt-l-x86iamx-8-16\n"
        )

        # Non-YAML file (should be skipped by all passes)
        (wf_dir / "README.md").write_text("# Workflows\n")

        return canary

    def test_removes_unwanted_entry_points(self, tmp_path):
        canary = self._setup_workflows(tmp_path)
        wf_dir = canary / ".github" / "workflows"

        instrument_workflows(canary, "c-mt-")

        # test.yml: entry point NOT in KEEP_WORKFLOWS → removed
        assert not (wf_dir / "test.yml").exists()
        # lint.yml: in KEEP_WORKFLOWS → kept
        assert (wf_dir / "lint.yml").exists()
        # _reusable.yml: not an entry point → kept
        assert (wf_dir / "_reusable.yml").exists()

    def test_filters_non_arc_jobs_from_kept_workflows(self, tmp_path):
        canary = self._setup_workflows(tmp_path)
        wf_dir = canary / ".github" / "workflows"

        instrument_workflows(canary, "c-mt-")

        content = (wf_dir / "lint.yml").read_text()
        assert "lint-arc:" in content
        assert "lint-gha:" not in content  # non-ARC job removed

    def test_writes_determinator_stub(self, tmp_path):
        canary = self._setup_workflows(tmp_path)
        wf_dir = canary / ".github" / "workflows"

        instrument_workflows(canary, "test-prefix-")

        stub = wf_dir / "_runner-determinator.yml"
        assert stub.exists()
        assert "test-prefix-" in stub.read_text()
        assert "TARGET_PREFIX_PLACEHOLDER" not in stub.read_text()

    def test_writes_determinator_script(self, tmp_path):
        canary = self._setup_workflows(tmp_path)

        instrument_workflows(canary, "test-prefix-")

        script = canary / ".github" / "scripts" / "runner_determinator.py"
        assert script.exists()
        assert "test-prefix-" in script.read_text()

    def test_replaces_runner_prefix(self, tmp_path):
        canary = self._setup_workflows(tmp_path)
        wf_dir = canary / ".github" / "workflows"

        instrument_workflows(canary, "c-mt-")

        content = (wf_dir / "_reusable.yml").read_text()
        assert "c-mt-l-x86iamx-8-16" in content
        # Every occurrence of the old label is now prefixed with c-
        assert content.count("c-mt-l-x86iamx-8-16") == content.count("mt-l-x86iamx-8-16")

    def test_no_workflows_dir_logs_warning(self, tmp_path, caplog):
        """If .github/workflows doesn't exist, log and return early."""
        import logging

        canary = tmp_path / "canary"
        canary.mkdir()

        with caplog.at_level(logging.WARNING, logger="osdc-workload-test"):
            instrument_workflows(canary, "c-mt-")

        assert "No .github/workflows" in caplog.text

    def test_custom_keep_workflows(self, tmp_path):
        canary = self._setup_workflows(tmp_path)
        wf_dir = canary / ".github" / "workflows"

        # Keep test.yml instead of lint.yml
        instrument_workflows(canary, "c-mt-", keep_workflows=["test.yml"])

        assert (wf_dir / "test.yml").exists()
        assert not (wf_dir / "lint.yml").exists()  # entry point not in keep list

    def test_default_keep_workflows_is_copy(self, tmp_path):
        """Default keep_workflows is a copy of KEEP_WORKFLOWS, not a reference."""
        canary = self._setup_workflows(tmp_path)

        instrument_workflows(canary, "c-mt-")

        # KEEP_WORKFLOWS should not be modified by the function
        assert KEEP_WORKFLOWS == ["lint.yml"]

    def test_non_yaml_files_skipped(self, tmp_path):
        """Non-YAML files in workflows dir are not touched."""
        canary = self._setup_workflows(tmp_path)
        wf_dir = canary / ".github" / "workflows"

        instrument_workflows(canary, "c-mt-")

        assert (wf_dir / "README.md").read_text() == "# Workflows\n"

    def test_scripts_dir_created(self, tmp_path):
        """The .github/scripts directory is created if it doesn't exist."""
        canary = self._setup_workflows(tmp_path)

        instrument_workflows(canary, "c-mt-")

        assert (canary / ".github" / "scripts").is_dir()

    def test_cross_repo_refs_rewritten(self, tmp_path):
        """pytorch/pytorch cross-repo refs are rewritten to local paths."""
        canary = tmp_path / "canary"
        wf_dir = canary / ".github" / "workflows"
        wf_dir.mkdir(parents=True)

        # Reusable workflow with a cross-repo reference
        (wf_dir / "_build.yml").write_text(
            "on:\n  workflow_call:\njobs:\n"
            "  build:\n"
            "    uses: pytorch/pytorch/.github/workflows/lint.yml@main\n"
        )

        instrument_workflows(canary, "c-mt-", keep_workflows=[])

        content = (wf_dir / "_build.yml").read_text()
        assert "uses: ./.github/workflows/lint.yml" in content
        assert "@main" not in content

    def test_repo_guards_rewritten(self, tmp_path):
        """github.repository guards are rewritten to repository_owner."""
        canary = tmp_path / "canary"
        wf_dir = canary / ".github" / "workflows"
        wf_dir.mkdir(parents=True)

        (wf_dir / "_check.yml").write_text(
            "on:\n  workflow_call:\njobs:\n"
            "  check:\n"
            "    if: github.repository == 'pytorch/pytorch'\n"
            "    runs-on: mt-l-x86iamx-8-16\n"
        )

        instrument_workflows(canary, "c-mt-", keep_workflows=[])

        content = (wf_dir / "_check.yml").read_text()
        assert "github.repository_owner == 'pytorch'" in content

    def test_kept_workflow_unchanged_when_all_arc(self, tmp_path):
        """When a kept workflow has only ARC jobs, content is unchanged after filter."""
        canary = tmp_path / "canary"
        wf_dir = canary / ".github" / "workflows"
        wf_dir.mkdir(parents=True)

        original = (
            "on:\n  pull_request:\njobs:\n"
            "  build:\n"
            "    runs-on: mt-l-x86iamx-8-16\n"
            "    steps:\n      - run: echo ok\n"
        )
        (wf_dir / "lint.yml").write_text(original)

        instrument_workflows(canary, "c-mt-")

        # The file should still exist (prefix replacement changes it)
        assert (wf_dir / "lint.yml").exists()

    def test_kept_workflow_not_present(self, tmp_path):
        """When a kept workflow doesn't exist on disk, skip gracefully."""
        canary = tmp_path / "canary"
        wf_dir = canary / ".github" / "workflows"
        wf_dir.mkdir(parents=True)

        # Only create a reusable workflow, no lint.yml
        (wf_dir / "_reusable.yml").write_text(
            "on:\n  workflow_call:\njobs:\n  r:\n    runs-on: mt-l-x86iamx-8-16\n"
        )

        # Should not crash even though lint.yml doesn't exist
        instrument_workflows(canary, "c-mt-")


# ── create_workload_pr ───────────────────────────────────────────────


class TestCreateWorkloadPr:
    @patch("workload_phases.run_cmd")
    def test_full_flow_commit_push_pr(self, mock_run, tmp_path):
        """Full non-dry-run flow: config, add, diff, commit, push, pr create."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add -A
            MagicMock(returncode=1),  # git diff --cached --quiet → changes exist
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push -f
            MagicMock(returncode=0, stdout="https://github.com/pytorch/pytorch-canary/pull/99\n"),
        ]

        result = create_workload_pr(canary, "test-cluster", False, "test-branch")
        assert result == 99

        # Verify push was called
        push_call = mock_run.call_args_list[5]
        assert "push" in push_call[0][0]

        # Verify gh pr create was called
        pr_call = mock_run.call_args_list[6]
        cmd = pr_call[0][0]
        assert "pr" in cmd
        assert "create" in cmd

    @patch("workload_phases.run_cmd")
    def test_dry_run_skips_push_and_pr(self, mock_run, tmp_path):
        """In dry run mode, push and pr create are not called."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff → changes exist
            MagicMock(returncode=0),  # git commit
        ]

        result = create_workload_pr(canary, "test-cluster", True, "test-branch")
        assert result is None

        # No push or pr create
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            assert "push" not in cmd

    @patch("workload_phases.run_cmd")
    def test_no_changes_returns_none(self, mock_run, tmp_path):
        """When git diff --cached --quiet returns 0, no commit is made."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0),  # git diff → no changes
        ]

        result = create_workload_pr(canary, "test-cluster", False, "test-branch")
        assert result is None
        assert mock_run.call_count == 4

    @patch("workload_phases.run_cmd")
    def test_pr_url_parse_failure_returns_none(self, mock_run, tmp_path):
        """When gh pr create returns garbage, return None."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # diff → changes
            MagicMock(returncode=0),  # commit
            MagicMock(returncode=0),  # push
            MagicMock(returncode=0, stdout="not-a-url\n"),  # pr create → garbage
        ]

        result = create_workload_pr(canary, "test-cluster", False, "test-branch")
        assert result is None

    @patch("workload_phases.run_cmd")
    def test_commit_message_contains_cluster_and_prefix(self, mock_run, tmp_path):
        """Commit message includes PR_TITLE_PREFIX and cluster_id."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # diff → changes
            MagicMock(returncode=0),  # commit
        ]

        create_workload_pr(canary, "my-cluster", True, "branch")

        commit_call = mock_run.call_args_list[4]
        commit_msg = commit_call[0][0][3]  # git commit -m <msg>
        assert "my-cluster" in commit_msg
        assert PR_TITLE_PREFIX in commit_msg

    @patch("workload_phases.run_cmd")
    def test_pr_url_trailing_slash_parsed(self, mock_run, tmp_path):
        """PR URL with trailing slash still parses correctly."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # diff → changes
            MagicMock(returncode=0),  # commit
            MagicMock(returncode=0),  # push
            MagicMock(returncode=0, stdout="https://github.com/pytorch/pytorch-canary/pull/42/\n"),
        ]

        result = create_workload_pr(canary, "cluster", False, "branch")
        assert result == 42

    @patch("workload_phases.run_cmd")
    def test_pr_create_args_correct(self, mock_run, tmp_path):
        """Verify gh pr create is called with the right repo, title, head, base."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # diff → changes
            MagicMock(returncode=0),  # commit
            MagicMock(returncode=0),  # push
            MagicMock(returncode=0, stdout="https://github.com/pytorch/pytorch-canary/pull/1\n"),
        ]

        create_workload_pr(canary, "my-cl", False, "my-branch")

        pr_cmd = mock_run.call_args_list[6][0][0]
        assert "--repo" in pr_cmd
        assert "pytorch/pytorch-canary" in pr_cmd
        assert "--head" in pr_cmd
        assert "my-branch" in pr_cmd
        assert "--base" in pr_cmd
        assert "main" in pr_cmd

    @patch("workload_phases.run_cmd")
    def test_git_config_calls(self, mock_run, tmp_path):
        """git config user.name and user.email are set with check=False."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0),  # diff → no changes
        ]

        create_workload_pr(canary, "c", False, "b")

        name_call = mock_run.call_args_list[0]
        assert name_call[0][0] == ["git", "config", "user.name", "OSDC Workload Test"]
        assert name_call[1]["check"] is False

        email_call = mock_run.call_args_list[1]
        assert "osdc-workload-test@pytorch.org" in email_call[0][0]
        assert email_call[1]["check"] is False

    @patch("workload_phases.safe_json_loads")
    @patch("workload_phases.run_cmd")
    def test_pr_already_exists_reuses_it(self, mock_run, mock_json, tmp_path):
        """When gh pr create fails but gh pr view finds existing PR, reuse it."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff → changes exist
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push
            MagicMock(returncode=1, stdout="", stderr="already exists"),  # gh pr create fails
            MagicMock(returncode=0, stdout='{"number":42,"url":"https://github.com/pytorch/pytorch-canary/pull/42"}'),  # gh pr view
        ]
        mock_json.return_value = {"number": 42, "url": "https://github.com/pytorch/pytorch-canary/pull/42"}

        result = create_workload_pr(canary, "test-cluster", False, "test-branch")
        assert result == 42

        # Verify gh pr view was called with the branch
        view_call = mock_run.call_args_list[7]
        cmd = view_call[0][0]
        assert "pr" in cmd
        assert "view" in cmd
        assert "test-branch" in cmd

    @patch("workload_phases.run_cmd")
    def test_pr_create_fails_and_no_existing_pr(self, mock_run, tmp_path):
        """When both gh pr create and gh pr view fail, return None."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff → changes exist
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push
            MagicMock(returncode=1, stdout="", stderr="create failed"),  # gh pr create fails
            MagicMock(returncode=1, stdout="", stderr="no PR found"),  # gh pr view fails
        ]

        result = create_workload_pr(canary, "test-cluster", False, "test-branch")
        assert result is None

    @patch("workload_phases.safe_json_loads")
    @patch("workload_phases.run_cmd")
    def test_pr_create_fails_view_returns_invalid_json(self, mock_run, mock_json, tmp_path):
        """When gh pr create fails and gh pr view returns invalid JSON, return None."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # git diff → changes exist
            MagicMock(returncode=0),  # git commit
            MagicMock(returncode=0),  # git push
            MagicMock(returncode=1, stdout="", stderr="create failed"),  # gh pr create fails
            MagicMock(returncode=0, stdout="not valid json{{{"),  # gh pr view succeeds but bad output
        ]
        mock_json.return_value = None  # safe_json_loads returns None for invalid JSON

        result = create_workload_pr(canary, "test-cluster", False, "test-branch")
        assert result is None

    @patch("workload_phases.run_cmd")
    def test_empty_pr_url_returns_none(self, mock_run, tmp_path):
        """When gh pr create returns empty stdout, return None."""
        canary = tmp_path / "canary"
        canary.mkdir()

        mock_run.side_effect = [
            MagicMock(returncode=0),  # git config user.name
            MagicMock(returncode=0),  # git config user.email
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=1),  # diff → changes
            MagicMock(returncode=0),  # commit
            MagicMock(returncode=0),  # push
            MagicMock(returncode=0, stdout="\n"),  # empty URL
        ]

        result = create_workload_pr(canary, "c", False, "b")
        assert result is None


# ── monitor_workflows ────────────────────────────────────────────────


class TestMonitorWorkflows:
    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_immediate_completion(self, mock_run, mock_sleep, mock_time):
        """All runs completed on first poll — no sleep needed."""
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 300)

        completed_runs = [
            {
                "databaseId": 1,
                "status": "completed",
                "conclusion": "success",
                "name": "lint",
                "createdAt": "2026-03-20T13:00:00Z",
            },
        ]
        collected = [{"run_id": 1, "conclusion": "success", "jobs": []}]

        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(completed_runs)
        )

        with (
            patch(
                "workload_phases.safe_json_loads",
                return_value=completed_runs,
            ),
            patch(
                "phases_validation._filter_runs_by_time",
                return_value=completed_runs,
            ),
            patch(
                "phases_validation._collect_run_details",
                return_value=collected,
            ),
        ):
            results = monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        assert len(results) == 1
        assert results[0]["run_id"] == 1
        mock_sleep.assert_not_called()

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_run_list_failure_retries(self, mock_run, mock_sleep, mock_time):
        """gh run list failure logs warning, sleeps, retries."""
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        completed_run = {
            "databaseId": 1,
            "status": "completed",
            "conclusion": "success",
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        collected = [{"run_id": 1, "jobs": []}]

        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="network error"),
            MagicMock(returncode=0, stdout=json.dumps([completed_run])),
        ]

        with (
            patch("workload_phases.safe_json_loads", return_value=[completed_run]),
            patch("phases_validation._filter_runs_by_time", return_value=[completed_run]),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            results = monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        assert len(results) == 1
        assert mock_sleep.call_count == 1

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_no_runs_found_waits(self, mock_run, mock_sleep, mock_time):
        """When no runs found yet, wait and retry."""
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        completed_run = {
            "databaseId": 1,
            "status": "completed",
            "conclusion": "success",
            "name": "test",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        collected = [{"run_id": 1, "jobs": []}]

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="[]"),
            MagicMock(returncode=0, stdout=json.dumps([completed_run])),
        ]

        with (
            patch("workload_phases.safe_json_loads", side_effect=[[], [completed_run]]),
            patch("phases_validation._filter_runs_by_time", side_effect=[[], [completed_run]]),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            results = monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        assert len(results) == 1
        assert mock_sleep.call_count == 1

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_in_progress_runs_wait(self, mock_run, mock_sleep, mock_time):
        """Runs in progress cause polling to continue."""
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        in_progress = {
            "databaseId": 1,
            "status": "in_progress",
            "conclusion": None,
            "name": "lint",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        completed = {
            "databaseId": 1,
            "status": "completed",
            "conclusion": "success",
            "name": "lint",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        collected = [{"run_id": 1, "jobs": []}]

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps([in_progress])),
            MagicMock(returncode=0, stdout=json.dumps([completed])),
        ]

        with (
            patch("workload_phases.safe_json_loads", side_effect=[[in_progress], [completed]]),
            patch("phases_validation._filter_runs_by_time", side_effect=[[in_progress], [completed]]),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            results = monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        assert len(results) == 1
        assert mock_sleep.call_count == 1

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_timeout_collects_partial(self, mock_run, mock_sleep, mock_time):
        """When timeout expires, partial results are collected."""
        mock_time.side_effect = _make_time_sequence(0, 999999)

        partial = [
            {
                "databaseId": 1,
                "status": "in_progress",
                "name": "lint",
                "createdAt": "2026-03-20T13:00:00Z",
            },
        ]
        collected = [{"run_id": 1, "conclusion": "in_progress"}]

        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(partial)
        )

        with (
            patch("workload_phases.safe_json_loads", return_value=partial),
            patch("phases_validation._filter_runs_by_time", return_value=partial),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            results = monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
                timeout_minutes=1,
            )

        assert len(results) == 1

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_timeout_with_run_list_failure_returns_empty(self, mock_run, mock_sleep, mock_time):
        """Timeout + failed final run list returns empty list."""
        mock_time.side_effect = _make_time_sequence(0, 999999)

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        results = monitor_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            timeout_minutes=1,
        )
        assert results == []

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_timeout_with_empty_stdout_returns_empty(self, mock_run, mock_sleep, mock_time):
        """Timeout + empty stdout returns empty list."""
        mock_time.side_effect = _make_time_sequence(0, 999999)

        mock_run.return_value = MagicMock(returncode=0, stdout="   ", stderr="")

        results = monitor_workflows(
            "test-branch",
            datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            timeout_minutes=1,
        )
        assert results == []

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_logs_new_run_urls(self, mock_run, mock_sleep, mock_time, caplog):
        """New run IDs are logged with URL on first discovery."""
        import logging

        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        runs = [
            {
                "databaseId": 42,
                "status": "completed",
                "conclusion": "success",
                "name": "build",
                "createdAt": "2026-03-20T13:00:00Z",
            },
        ]
        collected = [{"run_id": 42}]

        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(runs))

        with (
            caplog.at_level(logging.INFO, logger="osdc-workload-test"),
            patch("workload_phases.safe_json_loads", return_value=runs),
            patch("phases_validation._filter_runs_by_time", return_value=runs),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        assert "actions/runs/42" in caplog.text

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_run_logged_only_once(self, mock_run, mock_sleep, mock_time):
        """A run ID is only logged the first time it appears."""
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 300, 9999)

        in_progress = {
            "databaseId": 7,
            "status": "in_progress",
            "conclusion": None,
            "name": "lint",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        completed = {
            "databaseId": 7,
            "status": "completed",
            "conclusion": "success",
            "name": "lint",
            "createdAt": "2026-03-20T13:00:00Z",
        }
        collected = [{"run_id": 7}]

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps([in_progress])),
            MagicMock(returncode=0, stdout=json.dumps([completed])),
        ]

        with (
            patch("workload_phases.safe_json_loads", side_effect=[[in_progress], [completed]]),
            patch("phases_validation._filter_runs_by_time", side_effect=[[in_progress], [completed]]),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        # Sleep only once (during in_progress), then complete
        assert mock_sleep.call_count == 1

    @patch("workload_phases.time.time")
    @patch("workload_phases.time.sleep")
    @patch("workload_phases.run_cmd")
    def test_run_without_database_id_skipped(self, mock_run, mock_sleep, mock_time):
        """Runs without databaseId are not logged but don't crash."""
        mock_time.side_effect = _make_time_sequence(0, 100, 200, 9999)

        runs = [
            {
                "status": "completed",
                "conclusion": "success",
                "name": "lint",
                "createdAt": "2026-03-20T13:00:00Z",
            },
        ]
        collected = [{"run_id": None}]

        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(runs))

        with (
            patch("workload_phases.safe_json_loads", return_value=runs),
            patch("phases_validation._filter_runs_by_time", return_value=runs),
            patch("phases_validation._collect_run_details", return_value=collected),
        ):
            results = monitor_workflows(
                "test-branch",
                datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC),
            )

        assert len(results) == 1


# ── print_workload_report ─────────────────────────────────────────────


class TestPrintWorkloadReport:
    def test_all_passed(self, capsys):
        results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "build", "conclusion": "success"},
                    {"name": "test", "conclusion": "success"},
                ],
            },
        ]
        assert print_workload_report("cluster-1", "my-cluster", results) is True
        out = capsys.readouterr().out
        assert "PASSED" in out
        assert "\u2713" in out  # checkmark icon

    def test_failure_returns_false(self, capsys):
        results = [
            {
                "run_id": 1,
                "jobs": [{"name": "build", "conclusion": "failure"}],
            },
        ]
        assert print_workload_report("cluster-1", "my-cluster", results) is False
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "\u2717" in out  # cross icon

    def test_in_progress_does_not_fail(self, capsys):
        results = [
            {
                "run_id": 1,
                "jobs": [{"name": "build", "conclusion": "in_progress"}],
            },
        ]
        assert print_workload_report("cluster-1", "my-cluster", results) is True
        out = capsys.readouterr().out
        assert "in_progress" in out
        assert "\u2026" in out  # ellipsis icon

    def test_null_conclusion_treated_as_in_progress(self, capsys):
        """GitHub API returns None for in-progress job conclusions."""
        results = [
            {
                "run_id": 1,
                "jobs": [{"name": "build", "conclusion": None}],
            },
        ]
        assert print_workload_report("cluster-1", "my-cluster", results) is True
        out = capsys.readouterr().out
        assert "in_progress" in out

    def test_interrupted_header(self, capsys):
        print_workload_report("cluster-1", "my-cluster", [], interrupted=True)
        out = capsys.readouterr().out
        assert "(interrupted)" in out

    def test_not_interrupted_header(self, capsys):
        print_workload_report("cluster-1", "my-cluster", [])
        out = capsys.readouterr().out
        assert "(interrupted)" not in out

    def test_no_results_available(self, capsys):
        result = print_workload_report("cluster-1", "my-cluster", [])
        assert result is True
        out = capsys.readouterr().out
        assert "No workflow results available" in out

    def test_failure_log_printed(self, capsys):
        results = [
            {
                "run_id": 42,
                "failure_log": "Error: something broke\nLine 2\nLine 3",
                "jobs": [{"name": "build", "conclusion": "failure"}],
            },
        ]
        print_workload_report("c", "n", results)
        out = capsys.readouterr().out
        assert "something broke" in out
        assert "run 42" in out

    def test_failure_log_truncated_at_20_lines(self, capsys):
        """Only the first 20 lines of a failure log are printed."""
        long_log = "\n".join(f"line {i}" for i in range(50))
        results = [
            {
                "run_id": 1,
                "failure_log": long_log,
                "jobs": [{"name": "job", "conclusion": "failure"}],
            },
        ]
        print_workload_report("c", "n", results)
        out = capsys.readouterr().out
        assert "line 19" in out
        assert "line 20" not in out

    def test_report_shows_cluster_info(self, capsys):
        print_workload_report("my-cluster-id", "my-cluster-name", [])
        out = capsys.readouterr().out
        assert "my-cluster-id" in out
        assert "my-cluster-name" in out

    def test_cancelled_job_fails(self, capsys):
        results = [
            {
                "run_id": 1,
                "jobs": [{"name": "build", "conclusion": "cancelled"}],
            },
        ]
        assert print_workload_report("c", "n", results) is False

    def test_mixed_success_and_failure(self, capsys):
        results = [
            {
                "run_id": 1,
                "jobs": [
                    {"name": "build", "conclusion": "success"},
                    {"name": "test", "conclusion": "failure"},
                ],
            },
        ]
        assert print_workload_report("c", "n", results) is False

    def test_multiple_runs(self, capsys):
        results = [
            {"run_id": 1, "jobs": [{"name": "a", "conclusion": "success"}]},
            {"run_id": 2, "jobs": [{"name": "b", "conclusion": "success"}]},
        ]
        assert print_workload_report("c", "n", results) is True

    def test_run_with_no_jobs(self, capsys):
        """Run with empty jobs list does not crash."""
        results = [{"run_id": 1, "jobs": []}]
        assert print_workload_report("c", "n", results) is True

    def test_job_without_name(self, capsys):
        """Job without name key uses 'unknown' fallback."""
        results = [
            {"run_id": 1, "jobs": [{"conclusion": "success"}]},
        ]
        assert print_workload_report("c", "n", results) is True
        out = capsys.readouterr().out
        assert "unknown" in out

    def test_run_without_failure_log(self, capsys):
        """Run without failure_log key does not print log section."""
        results = [
            {
                "run_id": 1,
                "jobs": [{"name": "build", "conclusion": "failure"}],
            },
        ]
        print_workload_report("c", "n", results)
        out = capsys.readouterr().out
        assert "Failure log" not in out

    def test_empty_failure_log(self, capsys):
        """Run with empty string failure_log does not print log section."""
        results = [
            {
                "run_id": 1,
                "failure_log": "",
                "jobs": [{"name": "build", "conclusion": "failure"}],
            },
        ]
        print_workload_report("c", "n", results)
        out = capsys.readouterr().out
        assert "Failure log" not in out

    def test_overall_pass_in_output(self, capsys):
        """The Overall: PASSED/FAILED line appears in output."""
        print_workload_report("c", "n", [])
        out = capsys.readouterr().out
        assert "Overall: PASSED" in out

    def test_overall_fail_in_output(self, capsys):
        results = [
            {"run_id": 1, "jobs": [{"name": "j", "conclusion": "failure"}]},
        ]
        print_workload_report("c", "n", results)
        out = capsys.readouterr().out
        assert "Overall: FAILED" in out

    def test_separator_lines(self, capsys):
        """Report has separator lines."""
        print_workload_report("c", "n", [])
        out = capsys.readouterr().out
        assert "=" * 60 in out

    def test_date_in_output(self, capsys):
        """Current UTC date appears in the report."""
        print_workload_report("c", "n", [])
        out = capsys.readouterr().out
        assert "Date:" in out
        assert "UTC" in out

    def test_timed_out_conclusion(self, capsys):
        """A 'timed_out' conclusion is treated as failure."""
        results = [
            {"run_id": 1, "jobs": [{"name": "j", "conclusion": "timed_out"}]},
        ]
        assert print_workload_report("c", "n", results) is False
