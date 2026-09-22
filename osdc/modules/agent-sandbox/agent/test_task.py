"""Tests for the one-shot task entrypoint (task.py).

The dispatcher passes the spec as env vars and reads the result back from the pod's
log, so the contract this file pins is exactly that: env in, one line of JSON out, and
an exit status that separates "the task has an answer" from "the Job was built wrong".
"""

import json

import pytest
import sandbox
import task


class TestSpecFromEnv:
    def test_maps_env_to_spec_keys(self):
        spec = task.spec_from_env(
            {
                "SANDBOX_REPO": "org/repo",
                "SANDBOX_REF": "release/2.9",
                "SANDBOX_TASK": "summarize",
                "SANDBOX_MODEL": "us.anthropic.x",
                "PATH": "/usr/bin",
            }
        )
        assert spec == {"repo": "org/repo", "ref": "release/2.9", "task": "summarize", "model": "us.anthropic.x"}

    def test_empty_values_are_dropped_so_run_task_defaults_apply(self):
        """The dispatcher sets every var, empty when the caller omitted it — passing
        "" through would clone the empty ref instead of main."""
        spec = task.spec_from_env({"SANDBOX_REPO": "org/repo", "SANDBOX_REF": "", "SANDBOX_TASK": ""})
        assert spec == {"repo": "org/repo"}

    def test_missing_repo_raises(self):
        with pytest.raises(KeyError):
            task.spec_from_env({"SANDBOX_TASK": "no repo"})


class TestMain:
    def test_prints_one_json_line_and_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setenv("SANDBOX_REPO", "org/repo")
        monkeypatch.setattr(task, "run_task", lambda spec: {"cloned": True, "report": f"ran {spec['repo']}"})
        assert task.main() == 0
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, f"the dispatcher parses the last JSON line; got {out}"
        assert json.loads(out[0]) == {"cloned": True, "report": "ran org/repo"}

    def test_captured_stage_errors_still_exit_zero(self, monkeypatch, capsys):
        """A failed clone or a refused Bedrock call is an answer, not a crash — a
        non-zero exit makes the dispatcher report a pod failure and lose the detail."""
        monkeypatch.setenv("SANDBOX_REPO", "org/private")
        monkeypatch.setattr(task, "run_task", lambda spec: {"cloned": False, "errors": {"clone": "not found"}})
        assert task.main() == 0
        assert json.loads(capsys.readouterr().out)["errors"]["clone"] == "not found"

    def test_missing_repo_exits_nonzero_without_printing_json(self, monkeypatch, capsys):
        monkeypatch.delenv("SANDBOX_REPO", raising=False)
        assert task.main() == 2
        captured = capsys.readouterr()
        assert captured.out.strip() == ""
        assert "SANDBOX_REPO" in captured.err

    def test_uses_the_real_run_task(self, monkeypatch):
        """Guard against the import drifting: task.py must call the library, not carry
        its own copy of the logic."""
        assert task.run_task is sandbox.run_task
