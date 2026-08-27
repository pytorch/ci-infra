"""Unit tests for `gpu-dev repro` (cli-tools/gpu-dev-cli/gpu_dev_cli/cli.py).

Focus:
  * ref parsing: pr/N, #N, bare number -> pull/N/merge with /head fallback;
    branch/sha -> generic fetch+checkout (with shlex.quote).
  * --no-connect CI path: run test, auto-cancel, exit code == test result.
  * --keep: never cancel.
  * the in-pod shell (remote) command construction + ssh hardening.

Everything is mocked: claim_direct / create_reservation / subprocess.run, and
sys.stdout.isatty so the connect branch is deterministic. No network/AWS/SSH.
"""
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.cli import main


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
USER_INFO = {"user_id": "u-123", "github_user": "octocat"}


def _patch_env(
    *,
    claim_result=None,
    create_result="res-id-deadbeef",
    isatty=False,
    auth_raises=None,
):
    """Build a stack of patch() context managers for the repro command.

    Returns (patches list, reservation_mgr MagicMock, subprocess_run MagicMock).
    Caller is responsible for entering/exiting the patches (use _run()).
    """
    rm = MagicMock(name="ReservationManager_instance")
    rm.claim_direct.return_value = claim_result
    rm.create_reservation.return_value = create_result
    rm.wait_for_reservation_completion.return_value = {"reservation_id": create_result}
    rm.get_connection_info.return_value = {
        "ssh_command": "ssh -p 30022 dev@1.2.3.4"
    }

    auth = MagicMock(name="authenticate_user")
    if auth_raises is not None:
        auth.side_effect = auth_raises
    else:
        auth.return_value = USER_INFO

    run = MagicMock(name="subprocess.run")
    # Default: test command exits 0
    run.return_value = MagicMock(returncode=0)

    patches = [
        patch("gpu_dev_cli.cli.load_config", MagicMock(return_value=MagicMock())),
        patch("gpu_dev_cli.cli.ReservationManager", MagicMock(return_value=rm)),
        patch("gpu_dev_cli.cli.authenticate_user", auth),
        patch("subprocess.run", run),
        # CliRunner swaps sys.stdout for a click wrapper during invoke, so
        # patch the wrapper's isatty (not the real sys.stdout instance).
        patch("click.testing._NamedTextIOWrapper.isatty", MagicMock(return_value=isatty)),
    ]
    return patches, rm, run


class _Ctx:
    """Enter a list of patches, expose rm/run, exit them on __exit__."""

    def __init__(self, patches, rm, run):
        self._patches = patches
        self.rm = rm
        self.run = run

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _run(cli_runner, args, **env):
    patches, rm, run = _patch_env(**env)
    with _Ctx(patches, rm, run) as ctx:
        result = cli_runner.invoke(main, ["repro", *args])
    return result, ctx.rm, ctx.run


def _remote_str(run_mock):
    """Extract the first subprocess.run command string (the test-run call)."""
    assert run_mock.call_args_list, "subprocess.run was never called"
    first = run_mock.call_args_list[0]
    # called as subprocess.run(cmd, shell=True)
    return first.args[0] if first.args else first.kwargs["args"]


# ---------------------------------------------------------------------------
# ref parsing
# ---------------------------------------------------------------------------
WARM = {"reservation_id": "warm0001abcd", "ssh_command": "ssh -p 30022 dev@1.2.3.4"}


def test_ref_pr_merged_uses_land_commit_else_merge_then_head(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/185264", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # merged PR -> the GitHub API merge_commit_sha (the actual trunk commit)
    assert "api.github.com/repos/pytorch/pytorch/pulls/185264" in cmd
    assert "merge_commit_sha" in cmd
    assert '"merged"' in cmd
    # open PR fallback: pull/N/merge then /head
    assert "pull/185264/merge" in cmd
    assert "pull/185264/head" in cmd
    # checkout uses the resolved fetch ref / sha (FETCH_HEAD strategy)
    assert "git checkout -f FETCH_HEAD" in cmd


def test_ref_hash_number_is_treated_as_pr(cli_runner):
    res, rm, run = _run(cli_runner, ["#99", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "pull/99/merge" in cmd
    assert "pull/99/head" in cmd


def test_ref_bare_number_is_treated_as_pr(cli_runner):
    res, rm, run = _run(cli_runner, ["42", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "pull/42/merge" in cmd
    assert "pull/42/head" in cmd


def test_ref_branch_resolves_via_lsremote_no_pr_path(cli_runner):
    res, rm, run = _run(cli_runner, ["my-feature-branch", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # branch path: resolve via ls-remote of the ref, fetch the resolved FREF
    assert "git ls-remote" in cmd and "my-feature-branch" in cmd
    assert "FREF=my-feature-branch" in cmd
    assert "git fetch origin \"$FREF\"" in cmd
    # not a PR path (no GitHub PR API / pull refs)
    assert "pull/" not in cmd and "api.github.com" not in cmd


def test_ref_sha_resolves_to_itself_no_pr_path(cli_runner):
    sha = "abc123def456"
    res, rm, run = _run(cli_runner, [sha, "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert sha in cmd
    assert "git fetch origin \"$FREF\"" in cmd
    assert "pull/" not in cmd and "api.github.com" not in cmd


def test_ref_with_shell_metachars_is_quoted(cli_runner):
    # A branch name with spaces / special chars must be shlex.quoted in the fetch.
    res, rm, run = _run(cli_runner, ["feat/x; rm -rf /", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # shlex.quote wraps the dangerous value in single quotes; the raw injection
    # 'rm -rf /' must appear only inside a quoted token, never as a bare command.
    assert "'feat/x; rm -rf /'" in cmd


def test_ref_is_stripped(cli_runner):
    res, rm, run = _run(cli_runner, ["  pr/7  ", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "pull/7/merge" in cmd


# ---------------------------------------------------------------------------
# in-pod remote command construction
# ---------------------------------------------------------------------------
def test_remote_command_structure(cli_runner):
    res, rm, run = _run(
        cli_runner,
        ["pr/1", "test/inductor/test_flex_attention.py", "TestX.test_y"],
        claim_result=WARM,
    )
    cmd = _remote_str(run)
    assert "cd /home/dev/pytorch" in cmd
    assert "safe.directory /home/dev/pytorch" in cmd
    assert "submodule update --init --recursive" in cmd
    # import torch guard -> rebuild (editable, no build isolation, streamed with -v)
    assert "import torch" in cmd
    assert "pip install" in cmd and "--no-build-isolation" in cmd and "-v" in cmd
    # PYTHONPATH-prefixed python invocation of the test args
    assert "PYTHONPATH=/home/dev/pytorch python test/inductor/test_flex_attention.py TestX.test_y" in cmd


# ---------------------------------------------------------------------------
# by-sha artifact cache (Phase 1/2): consume on hit, fill after a build
# ---------------------------------------------------------------------------
def test_remote_checks_bysha_cache_before_building(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/185264", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # resolves the ref to a concrete SHA, then probes the shared by-sha cache
    assert "git ls-remote" in cmd and "pull/185264/merge" in cmd
    assert "/ccache_shared/prebuilt/by-sha" in cmd
    assert "by-sha cache HIT" in cmd
    # the from-source fetch+build path is gated behind a cache miss
    assert 'if [ -z "$HIT" ]' in cmd


def test_remote_publishes_build_to_bysha(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # after a from-source build, publish the tree for the next dev (detached)
    assert "publish-pytorch-build" in cmd
    assert "setsid" in cmd


def test_bysha_resolve_for_branch_uses_lsremote_of_ref(cli_runner):
    res, rm, run = _run(cli_runner, ["my-feature-branch", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "git ls-remote" in cmd and "my-feature-branch" in cmd
    assert "/ccache_shared/prebuilt/by-sha" in cmd


def test_remote_uses_mold_linker_when_available(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # guarded mold -run wrapper on the rebuild (no-op until the image ships mold)
    assert "command -v mold" in cmd
    assert "mold -run" in cmd


def test_remote_requests_offpod_build_heartbeat_guarded(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    # heartbeat-guarded on-demand request to the build farm + shared bs() staging
    assert "build-queue" in cmd
    assert ".worker-alive" in cmd
    assert "$WANT.req" in cmd
    assert "bs()" in cmd
    # on-demand is gated behind a cache miss, before the in-pod build
    assert 'if [ -z "$HIT" ]' in cmd


def test_test_args_are_shlex_quoted(cli_runner):
    res, rm, run = _run(
        cli_runner,
        ["pr/1", "test/foo bar.py"],
        claim_result=WARM,
    )
    cmd = _remote_str(run)
    # the test arg with a space must be quoted
    assert "'test/foo bar.py'" in cmd


def test_ssh_command_hardened_when_missing_stricthostkey(cli_runner):
    # WARM ssh_command lacks StrictHostKeyChecking -> repro injects the flags.
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "StrictHostKeyChecking=no" in cmd
    assert "UserKnownHostsFile=/dev/null" in cmd
    assert "LogLevel=ERROR" in cmd


def test_ssh_command_not_double_hardened(cli_runner):
    already = {
        "reservation_id": "warm0001abcd",
        "ssh_command": "ssh -o StrictHostKeyChecking=no -p 30022 dev@1.2.3.4",
    }
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=already)
    cmd = _remote_str(run)
    # only one occurrence of StrictHostKeyChecking (no second injection)
    assert cmd.count("StrictHostKeyChecking") == 1


# ---------------------------------------------------------------------------
# warm vs cold reservation path
# ---------------------------------------------------------------------------
def test_warm_claim_skips_create_reservation(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM)
    rm.claim_direct.assert_called_once()
    rm.create_reservation.assert_not_called()
    assert "warm pod claimed" in res.output


def test_claim_direct_called_with_ref_none_and_repro_name(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py", "--gpus", "2", "--gpu-type", "h100"], claim_result=WARM)
    kwargs = rm.claim_direct.call_args.kwargs
    assert kwargs["gpu_count"] == 2
    assert kwargs["gpu_type"] == "h100"
    assert kwargs["name"] == "repro"
    assert kwargs["ref"] is None
    assert kwargs["github_user"] == "octocat"
    assert kwargs["user_id"] == "u-123"


def test_cold_path_when_no_warm_pod(cli_runner):
    # claim_direct returns None -> falls back to create_reservation + wait + conn.
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=None)
    rm.claim_direct.assert_called_once()
    rm.create_reservation.assert_called_once()
    kwargs = rm.create_reservation.call_args.kwargs
    assert kwargs["no_persistent_disk"] is True
    assert kwargs["name"] == "repro"
    rm.wait_for_reservation_completion.assert_called_once()
    rm.get_connection_info.assert_called_once()
    assert "no warm pod" in res.output


def test_cold_path_create_reservation_fails(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=None, create_result=None)
    assert "reservation failed" in res.output
    # never ran the test
    run.assert_not_called()


def test_claim_direct_exception_falls_through_to_cold(cli_runner):
    patches, rm, run = _patch_env(claim_result=None, create_result="cold-1234")
    rm.claim_direct.side_effect = RuntimeError("boom")
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py"])
    rm.create_reservation.assert_called_once()
    # subprocess.run still invoked once for the test using the cold ssh_command
    cmd = _remote_str(run)
    assert "ssh" in cmd


def test_no_ssh_connection_aborts(cli_runner):
    # ssh_command is the placeholder -> repro should bail before running the test.
    bad = {"reservation_id": "warm0001abcd", "ssh_command": "ssh user@placeholder"}
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=bad)
    assert "no SSH connection" in res.output
    run.assert_not_called()


# ---------------------------------------------------------------------------
# --no-connect CI path: auto-cancel + exit code
# ---------------------------------------------------------------------------
def test_no_connect_passing_test_exit_zero_and_autocancels(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py", "--no-connect"], claim_result=WARM, isatty=False)
    # test passed (returncode 0 default) -> exit 0
    assert res.exit_code == 0
    rm.cancel_reservation.assert_called_once()
    cancel_args = rm.cancel_reservation.call_args.args
    assert cancel_args[0] == "warm0001abcd"
    assert cancel_args[1] == "u-123"
    assert "cancelled repro box" in res.output


def test_no_connect_failing_test_propagates_exit_code(cli_runner):
    patches, rm, run = _patch_env(claim_result=WARM, isatty=False)
    run.return_value = MagicMock(returncode=7)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py", "--no-connect"])
    assert res.exit_code == 7
    rm.cancel_reservation.assert_called_once()
    # rich injects ANSI between tokens, so match on stable fragments only.
    assert "test failed" in res.output
    assert "repro exit code" in res.output


def test_non_tty_acts_as_ci_even_without_no_connect_flag(cli_runner):
    # isatty False and no --no-connect -> still the CI auto-cancel path (connect=False).
    patches, rm, run = _patch_env(claim_result=WARM, isatty=False)
    run.return_value = MagicMock(returncode=3)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py"])
    assert res.exit_code == 3
    rm.cancel_reservation.assert_called_once()


def test_no_connect_only_runs_test_once_no_shell(cli_runner):
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py", "--no-connect"], claim_result=WARM, isatty=False)
    # exactly one subprocess.run (the test); no interactive shell spawned
    assert run.call_count == 1


# ---------------------------------------------------------------------------
# --keep
# ---------------------------------------------------------------------------
def test_keep_in_ci_path_does_not_cancel(cli_runner):
    res, rm, run = _run(
        cli_runner, ["pr/1", "test/foo.py", "--no-connect", "--keep"], claim_result=WARM, isatty=False
    )
    rm.cancel_reservation.assert_not_called()
    assert "kept" in res.output
    assert res.exit_code == 0


def test_keep_in_connect_path_does_not_prompt_or_cancel(cli_runner):
    # TTY connect path + --keep: spawns the shell, never prompts/cancels.
    patches, rm, run = _patch_env(claim_result=WARM, isatty=True)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py", "--keep"])
    rm.cancel_reservation.assert_not_called()
    assert "left" in res.output
    # two subprocess.run: the test + the interactive shell
    assert run.call_count == 2


# ---------------------------------------------------------------------------
# connect (TTY) path
# ---------------------------------------------------------------------------
def test_connect_path_prompts_and_cancels_on_yes(cli_runner):
    patches, rm, run = _patch_env(claim_result=WARM, isatty=True)
    with _Ctx(patches, rm, run):
        # click.confirm reads stdin -> "y" cancels
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py"], input="y\n")
    rm.cancel_reservation.assert_called_once()
    assert "cancelled" in res.output


def test_connect_path_keeps_box_on_no(cli_runner):
    patches, rm, run = _patch_env(claim_result=WARM, isatty=True)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py"], input="n\n")
    rm.cancel_reservation.assert_not_called()
    assert "left" in res.output


def test_connect_path_spawns_login_shell_at_pytorch(cli_runner):
    patches, rm, run = _patch_env(claim_result=WARM, isatty=True)
    with _Ctx(patches, rm, run):
        cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py"], input="y\n")
    # second subprocess.run is the interactive shell command
    shell_cmd = run.call_args_list[1].args[0]
    assert "cd /home/dev/pytorch" in shell_cmd
    assert "exec ${SHELL:-bash} -l" in shell_cmd
    assert " -t " in shell_cmd


# ---------------------------------------------------------------------------
# auth failure / edge
# ---------------------------------------------------------------------------
def test_auth_failure_returns_without_reserving(cli_runner):
    res, rm, run = _run(
        cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM, auth_raises=RuntimeError("no keys")
    )
    rm.claim_direct.assert_not_called()
    rm.create_reservation.assert_not_called()
    assert "no keys" in res.output


def test_test_args_required(cli_runner):
    # no test_args and no --lint -> bail with exit 2
    patches, rm, run = _patch_env(claim_result=WARM)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1"])
    assert res.exit_code == 2
    rm.claim_direct.assert_not_called()


# ---------------------------------------------------------------------------
# --lint (CPU lintrunner path)
# ---------------------------------------------------------------------------
def test_lint_defaults_to_cpu_x86_with_zero_gpus(cli_runner):
    # --lint with no extra args: CPU box, gpu_count must be 0 (lambda rejects CPU+gpus>0).
    res, rm, run = _run(cli_runner, ["--lint", "pr/1"], claim_result=WARM)
    kwargs = rm.claim_direct.call_args.kwargs
    assert kwargs["gpu_type"] == "cpu-x86"
    assert kwargs["gpu_count"] == 0
    assert kwargs["name"] == "repro"


def test_lint_remote_mirrors_ci_pr_diff_no_clang(cli_runner):
    res, rm, run = _run(cli_runner, ["--lint", "pr/1"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "lintrunner init" in cmd
    # CI codegen (version + type stubs) so mypy/pyrefly are accurate
    assert "tools.generate_torch_version" in cmd
    assert "tools.pyi.gen_pyi" in cmd
    # python/general linters on the PR diff, clang linters skipped by default
    assert "--skip CLANGTIDY,CLANGTIDY_EXECUTORCH_COMPATIBILITY,CLANGFORMAT --merge-base-with origin/main" in cmd
    assert "--take CLANGTIDY,CLANGFORMAT" not in cmd
    assert "add --clang to run them" in cmd
    # no torch build / no python test on the lint path
    assert "pip install --break-system-packages -e ." not in cmd
    assert "PYTHONPATH=/home/dev/pytorch python" not in cmd


def test_lint_clang_flag_runs_cpp_linters(cli_runner):
    res, rm, run = _run(cli_runner, ["--lint", "--clang", "pr/1"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "tools.linter.clang_tidy.generate_build_files" in cmd
    assert "--take CLANGTIDY,CLANGFORMAT --merge-base-with origin/main" in cmd


def test_lint_passes_extra_args_as_scope(cli_runner):
    # extra args (ignore_unknown_options) override the scope.
    res, rm, run = _run(cli_runner, ["--lint", "pr/1", "--all-files"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "--skip CLANGTIDY,CLANGTIDY_EXECUTORCH_COMPATIBILITY,CLANGFORMAT --all-files" in cmd
    assert "--merge-base-with" not in cmd


def test_lint_run_uses_pty_for_progress(cli_runner):
    # lint runs over an SSH PTY (-t) so lintrunner renders its live progress bar.
    res, rm, run = _run(cli_runner, ["--lint", "pr/1"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "ssh -t " in cmd


def test_test_run_has_no_pty(cli_runner):
    # the python-test path must NOT allocate a PTY for the run.
    res, rm, run = _run(cli_runner, ["pr/1", "test/foo.py"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "ssh -t " not in cmd


def test_lint_explicit_cpu_arm_keeps_zero_gpus(cli_runner):
    res, rm, run = _run(cli_runner, ["--lint", "--gpu-type", "cpu-arm", "pr/1"], claim_result=WARM)
    kwargs = rm.claim_direct.call_args.kwargs
    assert kwargs["gpu_type"] == "cpu-arm"
    assert kwargs["gpu_count"] == 0


def test_lint_verdict_says_lint(cli_runner):
    patches, rm, run = _patch_env(claim_result=WARM, isatty=False)
    run.return_value = MagicMock(returncode=5)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "--lint", "pr/1", "--no-connect"])
    assert res.exit_code == 5
    assert "lint failed" in res.output


def test_lint_no_ref_lints_main_all_files(cli_runner):
    # bare `repro --lint` -> ref defaults to main, scope defaults to --all-files.
    res, rm, run = _run(cli_runner, ["--lint"], claim_result=WARM)
    kwargs = rm.claim_direct.call_args.kwargs
    assert kwargs["gpu_type"] == "cpu-x86"
    assert kwargs["gpu_count"] == 0
    cmd = _remote_str(run)
    assert "--skip CLANGTIDY,CLANGTIDY_EXECUTORCH_COMPATIBILITY,CLANGFORMAT --all-files" in cmd
    assert "--merge-base-with" not in cmd


def test_lint_branch_ref_defaults_to_all_files(cli_runner):
    # a non-PR ref (branch/sha) lints everything, not the empty merge-base diff.
    res, rm, run = _run(cli_runner, ["--lint", "main"], claim_result=WARM)
    cmd = _remote_str(run)
    assert "--skip CLANGTIDY,CLANGTIDY_EXECUTORCH_COMPATIBILITY,CLANGFORMAT --all-files" in cmd


def test_test_path_requires_ref(cli_runner):
    # without --lint, a missing ref bails (exit 2) before reserving anything.
    patches, rm, run = _patch_env(claim_result=WARM)
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro"])
    assert res.exit_code == 2
    rm.claim_direct.assert_not_called()


def test_cancel_failure_in_ci_is_caught(cli_runner):
    patches, rm, run = _patch_env(claim_result=WARM, isatty=False)
    rm.cancel_reservation.side_effect = RuntimeError("api down")
    with _Ctx(patches, rm, run):
        res = cli_runner.invoke(main, ["repro", "pr/1", "test/foo.py", "--no-connect"])
    # auto-cancel failure must not crash; message surfaced, exit code still the test's
    assert "auto-cancel failed" in res.output
    assert res.exit_code == 0
