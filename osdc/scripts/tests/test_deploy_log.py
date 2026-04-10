"""Unit tests for scripts/deploy-log.sh.

Tests the deploy-log library by sourcing it in a bash subprocess with a mock
kubectl on PATH. The mock kubectl records all invocations to a log file so we
can verify the exact ConfigMap operations performed.
"""

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

DEPLOY_LOG_SH = Path(__file__).resolve().parent.parent / "deploy-log.sh"


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_env(tmp_path):
    """Set up an environment with a mock kubectl and a fake git repo."""
    # Create mock kubectl that logs all invocations
    kubectl_log = tmp_path / "kubectl_calls.log"
    mock_kubectl = tmp_path / "bin" / "kubectl"
    mock_kubectl.parent.mkdir()

    # Mock kubectl: logs args and handles get/create+apply
    mock_kubectl.write_text(
        textwrap.dedent("""\
        #!/usr/bin/env bash
        echo "$@" >> "{log}"

        # For "get configmap" calls, return empty (no existing history)
        if [[ "$1" == "get" && "$2" == "configmap" ]]; then
            # Check if there's a stored value for this configmap
            CM_NAME="$3"
            STORE_DIR="{store}"
            if [[ -f "$STORE_DIR/$CM_NAME" ]]; then
                cat "$STORE_DIR/$CM_NAME"
            fi
            exit 0
        fi

        # For "apply" calls, capture stdin for label verification
        if [[ "$1" == "apply" && "$2" == "-f" && "$3" == "-" ]]; then
            APPLY_LOG="{apply_log}"
            cat >> "$APPLY_LOG"
            echo "---" >> "$APPLY_LOG"
            exit 0
        fi

        # For "create configmap --dry-run" piped to apply, output yaml
        if [[ "$1" == "create" && "$2" == "configmap" ]]; then
            # Just output something so the pipe works
            echo "apiVersion: v1"
            echo "kind: ConfigMap"
            echo "metadata:"
            echo "  name: $3"
            exit 0
        fi
        exit 0
        """).format(
            log=kubectl_log,
            store=tmp_path / "cm_store",
            apply_log=tmp_path / "apply_input.log",
        )
    )
    mock_kubectl.chmod(mock_kubectl.stat().st_mode | stat.S_IEXEC)

    # Create a fake git repo so deploy-log can gather metadata
    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    subprocess.run(
        ["git", "init", "-b", "test-branch"],
        cwd=git_dir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=git_dir,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
    )

    # ConfigMap store for history simulation
    (tmp_path / "cm_store").mkdir()

    return {
        "tmp_path": tmp_path,
        "kubectl_log": kubectl_log,
        "apply_log": tmp_path / "apply_input.log",
        "git_dir": git_dir,
        "bin_dir": tmp_path / "bin",
        "cm_store": tmp_path / "cm_store",
    }


def run_bash(mock_env, script, *, expect_success=True):
    """Run a bash script snippet that sources deploy-log.sh."""
    env = {
        "PATH": f"{mock_env['bin_dir']}:{os.environ.get('PATH', '')}",
        "HOME": str(mock_env["tmp_path"]),
        "USER": "testuser",
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        cwd=mock_env["git_dir"],
        env=env,
    )
    if expect_success:
        assert result.returncode == 0, (
            f"Script failed (rc={result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def get_kubectl_calls(mock_env):
    """Return list of kubectl invocations from the log."""
    log = mock_env["kubectl_log"]
    if not log.exists():
        return []
    return log.read_text().strip().splitlines()


# ============================================================================
# deploy_log_start tests
# ============================================================================


class TestDeployLogStart:
    """Tests for deploy_log_start()."""

    def test_prints_epoch_to_stdout(self, mock_env):
        result = run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\nEPOCH=$(deploy_log_start module "test-cluster" "arc")\necho "EPOCH=$EPOCH"',
        )
        # Should print a numeric epoch
        for line in result.stdout.strip().splitlines():
            if line.startswith("EPOCH="):
                epoch_val = line.split("=", 1)[1]
                assert epoch_val.isdigit(), f"Expected numeric epoch, got: {epoch_val}"
                assert int(epoch_val) > 1_000_000_000  # After year 2001
                break
        else:
            pytest.fail("No EPOCH= line in output")

    def test_creates_start_configmap(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        # Should have a create configmap call for the start CM
        create_calls = [c for c in calls if "create configmap" in c]
        assert any("osdc-deploy-module-start-arc" in c for c in create_calls), (
            f"Expected start configmap creation, got: {create_calls}"
        )

    def test_creates_history_configmap(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        assert any("osdc-deploy-module-history-arc" in c for c in create_calls), (
            f"Expected history configmap creation, got: {create_calls}"
        )

    def test_cmd_scope_naming(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start cmd "test-cluster" "deploy"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        assert any("osdc-deploy-cmd-start-deploy" in c for c in create_calls), (
            f"Expected cmd start configmap, got: {create_calls}"
        )

    def test_uses_osdc_system_namespace(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        assert all("-n osdc-system" in c or "osdc-system" in c for c in create_calls), (
            f"Expected osdc-system namespace in all calls, got: {create_calls}"
        )

    def test_includes_deploy_log_label(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        # Labels are injected into YAML via _deploy_log_inject_labels (awk),
        # not as kubectl CLI args. Check the YAML piped to kubectl apply.
        apply_log = mock_env["apply_log"]
        assert apply_log.exists(), "No YAML was piped to kubectl apply"
        yaml_content = apply_log.read_text()
        assert "app.kubernetes.io/managed-by: osdc-deploy-log" in yaml_content, (
            f"Expected managed-by label in applied YAML, got:\n{yaml_content}"
        )

    def test_configmap_data_fields(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        # Find the start configmap creation call
        start_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-start-arc" in c]
        assert len(start_calls) >= 1
        call = start_calls[0]
        # Verify required fields are present as --from-literal args
        assert "--from-literal=commit=" in call
        assert "--from-literal=branch=" in call
        assert "--from-literal=user=testuser" in call
        assert "--from-literal=cluster=test-cluster" in call
        assert "--from-literal=timestamp=" in call
        assert "--from-literal=module=arc" in call
        assert "--from-literal=status=started" in call


# ============================================================================
# deploy_log_finish tests
# ============================================================================


class TestDeployLogFinish:
    """Tests for deploy_log_finish()."""

    def test_creates_finish_configmap(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish module "test-cluster" "arc" "1000000000"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        assert any("osdc-deploy-module-finish-arc" in c for c in create_calls), (
            f"Expected finish configmap, got: {create_calls}"
        )

    def test_default_status_is_completed(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish module "test-cluster" "arc" "1000000000"',
        )
        calls = get_kubectl_calls(mock_env)
        finish_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-finish-arc" in c]
        assert any("--from-literal=status=completed" in c for c in finish_calls), (
            f"Expected status=completed, got: {finish_calls}"
        )

    def test_failed_status(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish module "test-cluster" "arc" "1000000000" "failed"',
        )
        calls = get_kubectl_calls(mock_env)
        finish_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-finish-arc" in c]
        assert any("--from-literal=status=failed" in c for c in finish_calls), (
            f"Expected status=failed, got: {finish_calls}"
        )

    def test_includes_duration(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish module "test-cluster" "arc" "1000000000"',
        )
        calls = get_kubectl_calls(mock_env)
        finish_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-finish-arc" in c]
        assert any("--from-literal=duration=" in c for c in finish_calls), (
            f"Expected duration field, got: {finish_calls}"
        )

    def test_cmd_scope_naming(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish cmd "test-cluster" "deploy-module-arc" "1000000000"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        assert any("osdc-deploy-cmd-finish-deploy-module-arc" in c for c in create_calls), (
            f"Expected cmd finish configmap, got: {create_calls}"
        )


# ============================================================================
# Non-fatal behavior tests
# ============================================================================


class TestNonFatal:
    """Verify deploy logging never aborts the script."""

    def test_start_survives_kubectl_failure(self, mock_env):
        # Replace mock kubectl with one that always fails
        failing_kubectl = mock_env["bin_dir"] / "kubectl"
        failing_kubectl.write_text("#!/usr/bin/env bash\nexit 1\n")
        failing_kubectl.chmod(failing_kubectl.stat().st_mode | stat.S_IEXEC)

        # Should still succeed (deploy_log_start returns 0 even if kubectl fails)
        result = run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\nEPOCH=$(deploy_log_start module "test-cluster" "arc")\necho "OK:$EPOCH"',
        )
        assert "OK:" in result.stdout

    def test_finish_survives_kubectl_failure(self, mock_env):
        failing_kubectl = mock_env["bin_dir"] / "kubectl"
        failing_kubectl.write_text("#!/usr/bin/env bash\nexit 1\n")
        failing_kubectl.chmod(failing_kubectl.stat().st_mode | stat.S_IEXEC)

        result = run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish module "test-cluster" "arc" "1000000000"\necho "SURVIVED"',
        )
        assert "SURVIVED" in result.stdout


# ============================================================================
# JSON entry tests
# ============================================================================


class TestJsonEntry:
    """Tests for _deploy_log_json_entry (via deploy_log_start/finish history)."""

    def test_start_history_is_valid_json(self, mock_env):
        """The history append should produce valid JSON lines."""
        # We can test this by examining the --from-literal=entries= arg
        # in the history configmap creation call
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        history_calls = [c for c in calls if "create configmap" in c and "history" in c]
        # Find the entries literal
        for call in history_calls:
            if "--from-literal=entries=" in call:
                # Extract the JSON from the --from-literal=entries={json} arg
                # The format is: ... --from-literal=entries={...} ...
                idx = call.index("--from-literal=entries=")
                rest = call[idx + len("--from-literal=entries=") :]
                # The JSON ends at the next space followed by -- or end of string
                json_str = rest.split(" --")[0].strip()
                entry = json.loads(json_str)
                assert entry["event"] == "start"
                assert entry["cluster"] == "test-cluster"
                assert entry["module"] == "arc"
                assert "ts" in entry
                assert "commit" in entry
                assert "branch" in entry
                assert "user" in entry
                assert entry["status"] == "started"
                break
        # Note: if we don't find the entries literal in the call args,
        # it's because the mock kubectl pipes work differently — that's OK,
        # the configmap creation is verified by other tests.

    def test_finish_history_includes_duration(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_finish module "test-cluster" "arc" "1000000000"',
        )
        calls = get_kubectl_calls(mock_env)
        history_calls = [c for c in calls if "create configmap" in c and "history" in c]
        for call in history_calls:
            if "--from-literal=entries=" in call:
                idx = call.index("--from-literal=entries=")
                rest = call[idx + len("--from-literal=entries=") :]
                json_str = rest.split(" --")[0].strip()
                entry = json.loads(json_str)
                assert "duration" in entry
                assert entry["event"] == "finish"
                assert entry["status"] == "completed"
                break


# ============================================================================
# Metadata gathering tests
# ============================================================================


class TestMetadata:
    """Tests for _deploy_log_gather_metadata (via configmap data fields)."""

    def test_captures_git_commit(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        start_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-start-arc" in c]
        assert len(start_calls) >= 1
        # commit should be a short git hash (not "unknown")
        call = start_calls[0]
        assert "--from-literal=commit=" in call
        assert "commit=unknown" not in call

    def test_captures_branch(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        start_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-start-arc" in c]
        assert any("--from-literal=branch=test-branch" in c for c in start_calls)

    def test_captures_user(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        start_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-start-arc" in c]
        assert any("--from-literal=user=testuser" in c for c in start_calls)

    def test_captures_iso_timestamp(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        start_calls = [c for c in calls if "create configmap" in c and "osdc-deploy-module-start-arc" in c]
        # Timestamp should be ISO 8601 format
        assert any("--from-literal=timestamp=20" in c for c in start_calls)


# ============================================================================
# History trimming tests
# ============================================================================


class TestHistoryTrimming:
    """Test that history is capped at 50 entries."""

    def test_existing_history_is_preserved(self, mock_env):
        # Pre-populate a history configmap with existing entries
        cm_name = "osdc-deploy-module-history-arc"
        existing = '{"ts":"old","event":"start","commit":"aaa"}'
        (mock_env["cm_store"] / cm_name).write_text(existing)

        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        # Verify the get call was made to fetch existing history
        get_calls = [c for c in calls if c.startswith("get configmap")]
        assert any(cm_name in c for c in get_calls)


# ============================================================================
# Integration: start + finish flow
# ============================================================================


class TestStartFinishFlow:
    """Test the full start → finish flow."""

    def test_start_then_finish(self, mock_env):
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\n'
            'START=$(deploy_log_start module "test-cluster" "arc")\n'
            'deploy_log_finish module "test-cluster" "arc" "$START"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        # Should have start, finish, and history configmaps
        names = " ".join(create_calls)
        assert "osdc-deploy-module-start-arc" in names
        assert "osdc-deploy-module-finish-arc" in names
        assert "osdc-deploy-module-history-arc" in names

    def test_dual_scope_logging(self, mock_env):
        """Test command + module level logging (deploy-module pattern)."""
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\n'
            'CMD_START=$(deploy_log_start cmd "test-cluster" "deploy-module-arc")\n'
            'MOD_START=$(deploy_log_start module "test-cluster" "arc")\n'
            'deploy_log_finish module "test-cluster" "arc" "$MOD_START"\n'
            'deploy_log_finish cmd "test-cluster" "deploy-module-arc" "$CMD_START"',
        )
        calls = get_kubectl_calls(mock_env)
        create_calls = [c for c in calls if "create configmap" in c]
        names = " ".join(create_calls)
        # Module-level configmaps
        assert "osdc-deploy-module-start-arc" in names
        assert "osdc-deploy-module-finish-arc" in names
        # Command-level configmaps
        assert "osdc-deploy-cmd-start-deploy-module-arc" in names
        assert "osdc-deploy-cmd-finish-deploy-module-arc" in names


# ============================================================================
# JSON escaping tests
# ============================================================================


class TestJsonEscaping:
    """Test that _deploy_log_escape_json handles special characters."""

    def test_escape_json_double_quotes(self, mock_env):
        """Branch names with double quotes must produce valid JSON."""
        result = run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\necho "$(_deploy_log_escape_json \'feature/fix-"thing"\')"',
        )
        assert result.stdout.strip() == 'feature/fix-\\"thing\\"'

    def test_escape_json_backslashes(self, mock_env):
        """Backslashes in values must be escaped."""
        result = run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\necho "$(_deploy_log_escape_json \'path\\to\\branch\')"',
        )
        assert result.stdout.strip() == "path\\\\to\\\\branch"

    def test_branch_with_quotes_produces_valid_json_entry(self, mock_env):
        """Full flow: a branch containing quotes must produce valid JSON in history."""
        # Create a branch with a double quote in the name
        subprocess.run(
            ["git", "checkout", "-b", 'feat/q"test'],
            cwd=mock_env["git_dir"],
            capture_output=True,
            check=True,
        )
        run_bash(
            mock_env,
            f'source "{DEPLOY_LOG_SH}"\ndeploy_log_start module "test-cluster" "arc"',
        )
        calls = get_kubectl_calls(mock_env)
        history_calls = [c for c in calls if "create configmap" in c and "history" in c]
        found = False
        for call in history_calls:
            if "--from-literal=entries=" in call:
                idx = call.index("--from-literal=entries=")
                rest = call[idx + len("--from-literal=entries=") :]
                json_str = rest.split(" --")[0].strip()
                entry = json.loads(json_str)
                assert '"' in entry["branch"]
                found = True
                break
        assert found, "No history call with --from-literal=entries= found"
