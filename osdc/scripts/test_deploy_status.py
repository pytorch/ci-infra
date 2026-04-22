"""Tests for deploy-status.py."""

import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "deploy_status",
    Path(__file__).resolve().parent / "deploy-status.py",
)
deploy_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy_status)


# -- helpers --


def _make_cm(name, data):
    return {"metadata": {"name": name}, "data": data}


def _finish_cm(scope, name, **data):
    return _make_cm(f"osdc-deploy-{scope}-finish-{name}", data)


def _start_cm(scope, name, **data):
    return _make_cm(f"osdc-deploy-{scope}-start-{name}", data)


def _history_cm(scope, name, entries):
    return _make_cm(
        f"osdc-deploy-{scope}-history-{name}",
        {"entries": "\n".join(json.dumps(e) for e in entries)},
    )


# -- fmt_duration --


class TestFmtDuration:
    def test_seconds(self):
        assert deploy_status.fmt_duration("45") == "45s"

    def test_minutes_and_seconds(self):
        assert deploy_status.fmt_duration("125") == "2m5s"

    def test_exact_minute(self):
        assert deploy_status.fmt_duration("60") == "1m"

    def test_hours_and_minutes(self):
        assert deploy_status.fmt_duration("3665") == "1h1m"

    def test_exact_hour(self):
        assert deploy_status.fmt_duration("3600") == "1h"

    def test_zero(self):
        assert deploy_status.fmt_duration("0") == "0s"

    def test_invalid_string(self):
        assert deploy_status.fmt_duration("abc") == "-"

    def test_none(self):
        assert deploy_status.fmt_duration(None) == "-"

    def test_empty_string(self):
        assert deploy_status.fmt_duration("") == "-"


# -- parse_configmaps --


class TestParseConfigmaps:
    def test_finish_module(self):
        items = [_finish_cm("module", "arc-runners", commit="abc1234", status="completed", duration="245")]
        start, finish, history = deploy_status.parse_configmaps(items)
        assert ("module", "arc-runners") in finish
        assert finish[("module", "arc-runners")]["commit"] == "abc1234"
        assert not start
        assert not history

    def test_start_module(self):
        items = [_start_cm("module", "arc-runners", commit="abc1234", status="started")]
        start, finish, history = deploy_status.parse_configmaps(items)
        assert ("module", "arc-runners") in start
        assert not finish
        assert not history

    def test_history_module(self):
        entries = [
            {"ts": "2026-04-20T15:30:00Z", "event": "finish", "commit": "abc"},
            {"ts": "2026-04-19T10:00:00Z", "event": "finish", "commit": "def"},
        ]
        items = [_history_cm("module", "arc-runners", entries)]
        start, finish, history = deploy_status.parse_configmaps(items)
        assert len(history[("module", "arc-runners")]) == 2
        assert not start
        assert not finish

    def test_cmd_scope(self):
        items = [_finish_cm("cmd", "deploy", commit="abc", status="completed")]
        _, finish, _ = deploy_status.parse_configmaps(items)
        assert ("cmd", "deploy") in finish

    def test_ignores_unrelated_configmaps(self):
        items = [_make_cm("some-other-configmap", {"key": "val"})]
        start, finish, history = deploy_status.parse_configmaps(items)
        assert not start
        assert not finish
        assert not history

    def test_invalid_json_in_history_skipped(self):
        items = [
            _make_cm(
                "osdc-deploy-module-history-test",
                {
                    "entries": '{"valid": true}\nnot json\n{"also": "valid"}',
                },
            )
        ]
        _, _, history = deploy_status.parse_configmaps(items)
        assert len(history[("module", "test")]) == 2

    def test_empty_history_entries(self):
        items = [_make_cm("osdc-deploy-module-history-test", {"entries": ""})]
        _, _, history = deploy_status.parse_configmaps(items)
        assert history[("module", "test")] == []

    def test_module_name_with_hyphens(self):
        items = [_finish_cm("module", "arc-runners-b200", commit="abc", status="completed")]
        _, finish, _ = deploy_status.parse_configmaps(items)
        assert ("module", "arc-runners-b200") in finish

    def test_all_types_together(self):
        items = [
            _start_cm("module", "arc", commit="a", status="started", timestamp="T2"),
            _finish_cm("module", "arc", commit="a", status="completed", timestamp="T1"),
            _history_cm("module", "arc", [{"ts": "T1", "event": "finish"}]),
        ]
        start, finish, history = deploy_status.parse_configmaps(items)
        assert ("module", "arc") in start
        assert ("module", "arc") in finish
        assert ("module", "arc") in history

    def test_missing_data_key(self):
        items = [{"metadata": {"name": "osdc-deploy-module-finish-x"}}]
        _, finish, _ = deploy_status.parse_configmaps(items)
        assert ("module", "x") in finish
        assert finish[("module", "x")] == {}


# -- find_in_progress --


class TestFindInProgress:
    def test_in_progress(self):
        start = {("module", "arc"): {"timestamp": "2026-04-20T16:00:00Z"}}
        finish = {("module", "arc"): {"timestamp": "2026-04-20T15:00:00Z"}}
        assert deploy_status.find_in_progress(start, finish) == {("module", "arc")}

    def test_completed(self):
        start = {("module", "arc"): {"timestamp": "2026-04-20T15:00:00Z"}}
        finish = {("module", "arc"): {"timestamp": "2026-04-20T16:00:00Z"}}
        assert deploy_status.find_in_progress(start, finish) == set()

    def test_no_finish_yet(self):
        start = {("module", "arc"): {"timestamp": "2026-04-20T15:00:00Z"}}
        finish = {}
        assert deploy_status.find_in_progress(start, finish) == {("module", "arc")}

    def test_same_timestamp(self):
        start = {("module", "arc"): {"timestamp": "2026-04-20T15:00:00Z"}}
        finish = {("module", "arc"): {"timestamp": "2026-04-20T15:00:00Z"}}
        assert deploy_status.find_in_progress(start, finish) == set()


# -- colorize_status --


class TestColorizeStatus:
    def test_known_statuses(self):
        for status in ("completed", "failed", "started"):
            result = deploy_status.colorize_status(status)
            assert status in result

    def test_unknown_status(self):
        assert deploy_status.colorize_status("unknown") == "unknown"


# -- output integration --


def _capture_main(items, cluster="test-cluster", name_filter=None):
    """Run main() with given items and capture stdout."""
    data = json.dumps({"items": items})
    argv = ["deploy-status.py", cluster]
    if name_filter:
        argv.append(name_filter)

    buf = io.StringIO()
    with (
        patch.object(sys, "argv", argv),
        patch.object(sys, "stdin", io.StringIO(data)),
        patch.object(sys, "stdout", buf),
    ):
        deploy_status.main()
    return buf.getvalue()


class TestMainOutput:
    def test_empty_cluster(self):
        out = _capture_main([])
        assert "test-cluster" in out
        assert "No deploy records found" in out
        assert "No history records found" in out

    def test_current_state_shown(self):
        items = [
            _finish_cm(
                "module",
                "harbor",
                commit="abc1234",
                branch="main",
                user="alice",
                timestamp="2026-04-20T15:00:00Z",
                status="completed",
                duration="120",
                module="harbor",
            ),
        ]
        out = _capture_main(items)
        assert "harbor" in out
        assert "abc1234" in out
        assert "alice" in out
        assert "completed" in out
        assert "2m" in out

    def test_history_shown(self):
        entries = [
            {
                "ts": "2026-04-20T15:00:00Z",
                "event": "finish",
                "commit": "abc",
                "branch": "main",
                "user": "alice",
                "status": "completed",
                "duration": "60",
            },
        ]
        items = [_history_cm("module", "harbor", entries)]
        out = _capture_main(items)
        assert "harbor" in out
        assert "abc" in out
        assert "1m" in out

    def test_name_filter(self):
        items = [
            _finish_cm("module", "harbor", commit="abc", status="completed"),
            _finish_cm("module", "arc", commit="def", status="completed"),
        ]
        out = _capture_main(items, name_filter="harbor")
        assert "harbor" in out
        assert "abc" in out
        # arc should not appear in the current versions section
        assert "def" not in out

    def test_in_progress_shown(self):
        items = [
            _start_cm(
                "module",
                "arc",
                commit="new123",
                branch="feat",
                user="bob",
                timestamp="2026-04-20T16:00:00Z",
                status="started",
                module="arc",
            ),
            _finish_cm(
                "module",
                "arc",
                commit="old456",
                branch="main",
                user="alice",
                timestamp="2026-04-20T15:00:00Z",
                status="completed",
                duration="100",
                module="arc",
            ),
        ]
        out = _capture_main(items)
        assert "in progress" in out
        assert "new123" in out
        assert "Previous" in out
        assert "old456" in out

    def test_history_limit_default(self):
        entries = [
            {"ts": f"2026-04-{i:02d}T00:00:00Z", "event": "finish", "commit": f"c{i}", "status": "completed"}
            for i in range(1, 16)
        ]
        items = [_history_cm("module", "test", entries)]
        out = _capture_main(items)
        assert "last 10 of 15" in out

    def test_history_limit_filtered(self):
        entries = [
            {"ts": f"2026-04-{i:02d}T00:00:00Z", "event": "finish", "commit": f"c{i}", "status": "completed"}
            for i in range(1, 25)
        ]
        items = [_history_cm("module", "test", entries)]
        out = _capture_main(items, name_filter="test")
        assert "last 20 of 24" in out
