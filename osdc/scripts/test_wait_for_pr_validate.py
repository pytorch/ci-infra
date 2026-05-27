"""Unit tests for wait-for-pr-validate.py."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The module is named wait-for-pr-validate.py (hyphens), so load via importlib.
_spec = importlib.util.spec_from_file_location(
    "wait_for_pr_validate",
    Path(__file__).resolve().parent / "wait-for-pr-validate.py",
)
wait_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait_mod)


VALID_SHA = "a" * 40
VALID_REF = "renovate-runner/main-actions-runner"


@pytest.fixture(autouse=True)
def _required_env(monkeypatch, tmp_path):
    """Default env that satisfies _env() lookups for almost every test."""
    out_file = tmp_path / "GITHUB_OUTPUT"
    out_file.touch()
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("HEAD_SHA", VALID_SHA)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    # Make the poll loop fast.
    monkeypatch.setenv("POLL_INTERVAL_SEC", "0")
    monkeypatch.setenv("MAX_WAIT_SEC", "1")
    return out_file


def _read_output(path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_env_required(monkeypatch):
    monkeypatch.delenv("REPO", raising=False)
    with pytest.raises(SystemExit, match="REPO"):
        wait_mod._env("REPO")


def test_env_default(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    assert wait_mod._env("FOO", "bar") == "bar"


def test_env_returns_set_value(monkeypatch):
    monkeypatch.setenv("FOO", "baz")
    assert wait_mod._env("FOO", "bar") == "baz"


def test_int_env_default(monkeypatch):
    monkeypatch.delenv("FOO", raising=False)
    assert wait_mod._int_env("FOO", 42) == 42


def test_int_env_parses(monkeypatch):
    monkeypatch.setenv("FOO", "7")
    assert wait_mod._int_env("FOO", 42) == 7


def test_int_env_invalid(monkeypatch):
    monkeypatch.setenv("FOO", "not-a-number")
    with pytest.raises(SystemExit, match="must be an integer"):
        wait_mod._int_env("FOO", 1)


def test_emit_output_writes_to_file(_required_env):
    wait_mod._emit_output("approve", "all good")
    out = _read_output(_required_env)
    assert out == {"decision": "approve", "reason": "all good"}


def test_emit_output_without_github_output(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    wait_mod._emit_output("approve", "no file set")
    captured = capsys.readouterr()
    assert "decision=approve" in captured.out
    assert "reason=no file set" in captured.out


# ---------------------------------------------------------------------------
# _gh_api retry semantics
# ---------------------------------------------------------------------------


def _run_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_gh_api_success_first_try():
    with patch.object(wait_mod.subprocess, "run", return_value=_run_result(0, '{"ok":true}')) as m:
        result = wait_mod._gh_api(["repos/x/y"], retries=3, backoff=0)
    assert result == '{"ok":true}'
    assert m.call_count == 1


def test_gh_api_retries_on_transient_then_succeeds():
    sequence = [_run_result(1, stderr="500 server error"), _run_result(0, "ok")]
    with patch.object(wait_mod.subprocess, "run", side_effect=sequence), patch.object(wait_mod.time, "sleep"):
        result = wait_mod._gh_api(["x"], retries=3, backoff=0)
    assert result == "ok"


def test_gh_api_hard_fails_on_401():
    with (
        patch.object(wait_mod.subprocess, "run", return_value=_run_result(1, stderr="401 Bad credentials")),
        pytest.raises(RuntimeError, match="non-retryable"),
    ):
        wait_mod._gh_api(["x"], retries=5, backoff=0)


def test_gh_api_hard_fails_on_403_resource_not_accessible():
    with (
        patch.object(
            wait_mod.subprocess, "run", return_value=_run_result(1, stderr="Resource not accessible by integration")
        ),
        pytest.raises(RuntimeError, match="non-retryable"),
    ):
        wait_mod._gh_api(["x"], retries=5, backoff=0)


def test_gh_api_exhausts_retries():
    with (
        patch.object(wait_mod.subprocess, "run", return_value=_run_result(1, stderr="500")),
        patch.object(wait_mod.time, "sleep"),
        pytest.raises(RuntimeError, match="exhausted retries"),
    ):
        wait_mod._gh_api(["x"], retries=2, backoff=0)


# ---------------------------------------------------------------------------
# _gh_workflow_run
# ---------------------------------------------------------------------------


def test_gh_workflow_run_invokes_cli_correctly():
    with patch.object(wait_mod.subprocess, "run", return_value=_run_result(0)) as m:
        wait_mod._gh_workflow_run("o/r", "w.yml", "main", {"a": "1", "b": "2"})
    args = m.call_args[0][0]
    assert args[:2] == ["gh", "workflow"]
    assert "--ref" in args
    assert args[args.index("--ref") + 1] == "main"
    assert args.count("-f") == 2


def test_gh_workflow_run_raises_on_failure():
    with (
        patch.object(wait_mod.subprocess, "run", return_value=_run_result(1, stderr="nope")),
        pytest.raises(RuntimeError, match="gh workflow run failed"),
    ):
        wait_mod._gh_workflow_run("o/r", "w.yml", "main", {})


# ---------------------------------------------------------------------------
# _read_status
# ---------------------------------------------------------------------------


def test_read_status_missing_returns_empty():
    payload = '{"statuses": []}'
    with patch.object(wait_mod, "_gh_api", return_value=payload):
        state = wait_mod._read_status("o/r", VALID_SHA)
    assert state == ""


def test_read_status_returns_state():
    payload = (
        '{"statuses": [{"context": "osdc/pr-validate", "state": "pending", "updated_at": "2026-05-26T20:00:00Z"}]}'
    )
    with patch.object(wait_mod, "_gh_api", return_value=payload):
        state = wait_mod._read_status("o/r", VALID_SHA)
    assert state == "pending"


def test_read_status_picks_latest_when_multiple():
    payload = (
        '{"statuses": ['
        '{"context": "osdc/pr-validate", "state": "pending", "updated_at": "2026-01-01T00:00:00Z"},'
        '{"context": "osdc/pr-validate", "state": "success", "updated_at": "2026-05-26T00:00:00Z"},'
        '{"context": "other", "state": "failure", "updated_at": "2026-05-27T00:00:00Z"}'
        "]}"
    )
    with patch.object(wait_mod, "_gh_api", return_value=payload):
        state = wait_mod._read_status("o/r", VALID_SHA)
    assert state == "success"


# ---------------------------------------------------------------------------
# main() integration paths
# ---------------------------------------------------------------------------


def _pr_json(sha=VALID_SHA, ref=VALID_REF) -> str:
    return f'{{"head": {{"sha": "{sha}", "ref": "{ref}"}}}}'


def test_main_rejects_invalid_head_sha(_required_env, monkeypatch):
    monkeypatch.setenv("HEAD_SHA", "deadbeef")  # too short
    rc = wait_mod.main()
    assert rc == 0
    out = _read_output(_required_env)
    assert out["decision"] == "close-validation-failed"
    assert "40-char hex" in out["reason"]


def test_main_rejects_invalid_head_ref(_required_env):
    with patch.object(wait_mod, "_read_pr", return_value={"head": {"sha": VALID_SHA, "ref": "main"}}):
        rc = wait_mod.main()
    assert rc == 0
    out = _read_output(_required_env)
    assert out["decision"] == "close-branch-name-invalid"


def test_main_rejects_head_ref_with_dash_prefix(_required_env):
    with patch.object(wait_mod, "_read_pr", return_value={"head": {"sha": VALID_SHA, "ref": "-fno-good"}}):
        rc = wait_mod.main()
    assert rc == 0
    out = _read_output(_required_env)
    assert out["decision"] == "close-branch-name-invalid"


def test_main_happy_path_status_already_green(_required_env):
    with (
        patch.object(wait_mod, "_read_pr", return_value={"head": {"sha": VALID_SHA, "ref": VALID_REF}}),
        patch.object(wait_mod, "_read_status", return_value="success"),
    ):
        rc = wait_mod.main()
    out = _read_output(_required_env)
    assert rc == 0
    assert out["decision"] == "approve"


def test_main_dispatches_when_no_status_then_succeeds(_required_env):
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    states = ["", "success"]
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", side_effect=states),
        patch.object(wait_mod, "_dispatch_validate") as dispatch,
    ):
        rc = wait_mod.main()
    assert rc == 0
    out = _read_output(_required_env)
    assert out["decision"] == "approve"
    dispatch.assert_called_once_with("owner/repo", "42", VALID_SHA)


def test_main_closes_on_failure(_required_env):
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", return_value="failure"),
    ):
        rc = wait_mod.main()
    out = _read_output(_required_env)
    assert rc == 0
    assert out["decision"] == "close-validation-failed"


def test_main_closes_on_error(_required_env):
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", return_value="error"),
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "close-validation-failed"


def test_main_closes_on_head_moved(_required_env):
    pr_initial = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    new_sha = "b" * 40
    pr_moved = {"head": {"sha": new_sha, "ref": VALID_REF}}
    with patch.object(wait_mod, "_read_pr", side_effect=[pr_initial, pr_moved]):
        rc = wait_mod.main()
    out = _read_output(_required_env)
    assert rc == 0
    assert out["decision"] == "close-head-moved"
    assert new_sha in out["reason"]


def test_main_closes_on_dispatch_failure(_required_env):
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", return_value=""),
        patch.object(wait_mod, "_dispatch_validate", side_effect=RuntimeError("boom")),
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "close-dispatch-failed"


def test_main_timeout(_required_env, monkeypatch):
    monkeypatch.setenv("MAX_WAIT_SEC", "0")
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", return_value="pending"),
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "close-validation-timeout"


def test_main_polls_pending_until_success(_required_env):
    """No status initially → dispatch once; then pending pending success → approve, no re-dispatch."""
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    states = ["", "pending", "pending", "success"]
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", side_effect=states),
        patch.object(wait_mod, "_dispatch_validate") as dispatch,
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "approve"
    assert dispatch.call_count == 1


def test_main_unexpected_state_continues_polling(_required_env, capsys):
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    states = ["weird", "success"]
    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", side_effect=states),
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "approve"
    assert "unexpected" in capsys.readouterr().out


def test_main_recovers_from_transient_pr_read_failure(_required_env):
    """Single failure to re-read PR mid-poll should not abort the wait."""
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    pr_calls = [pr, RuntimeError("transient"), pr]
    states = ["pending", "success"]

    def pr_side(*_a, **_kw):
        r = pr_calls.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    with (
        patch.object(wait_mod, "_read_pr", side_effect=pr_side),
        patch.object(wait_mod, "_read_status", side_effect=states),
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "approve"


def test_main_recovers_from_transient_status_read_failure(_required_env):
    pr = {"head": {"sha": VALID_SHA, "ref": VALID_REF}}
    status_results = [RuntimeError("transient"), "success"]

    def status_side(*_a, **_kw):
        r = status_results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    with (
        patch.object(wait_mod, "_read_pr", return_value=pr),
        patch.object(wait_mod, "_read_status", side_effect=status_side),
        patch.object(wait_mod, "_dispatch_validate"),
    ):
        wait_mod.main()
    out = _read_output(_required_env)
    assert out["decision"] == "approve"


def test_dispatch_validate_uses_main_ref():
    with patch.object(wait_mod, "_gh_workflow_run") as m:
        wait_mod._dispatch_validate("o/r", "42", VALID_SHA)
    _, kwargs = m.call_args
    assert kwargs["ref"] == "main"
    assert kwargs["inputs"]["head_sha"] == VALID_SHA
    assert kwargs["inputs"]["pr_number"] == "42"
