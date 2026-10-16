"""Offline controller tests with mocked services and local process-cleanup regression checks."""

import argparse
import contextlib
import http.client
import io
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from unittest.mock import Mock, call

import drain_test_run as module
import pytest
import yaml
from drain_test_checks import CAPACITY_METRIC, CLUSTER, PROBE_LABEL, SCALE_SET_LABEL


@pytest.fixture
def controller(tmp_path):
    config = tmp_path / "clusters.yaml"
    config.write_text(f"clusters:\n  {CLUSTER}:\n    arc-runners:\n      runner_group: {CLUSTER}\n")
    args = argparse.Namespace(
        cluster_id=CLUSTER,
        root_dir=tmp_path,
        upstream_dir=tmp_path,
        clusters_yaml=config,
        dry_run=False,
        restore_only=False,
        start_timeout=1800,
        finish_timeout=1800,
        drain_timeout=600,
        observe_seconds=60,
    )
    test = module.DrainTest(args, "abc123")
    test.desired = {"cpu": 8}
    return test


def ars(limit=8):
    return [{"metadata": {"name": "cpu"}, "spec": {"maxRunners": limit, "runnerScaleSetName": PROBE_LABEL}}]


def job(phase="inflight", status="completed", conclusion="success", group=CLUSTER):
    return {"name": f"drain-{phase}", "status": status, "conclusion": conclusion, "runner_group_name": group}


def test_command_and_api_transport(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '{"ok":true}', ""))
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.api("pulls/3", {"body": "release"}, "PATCH") == {"ok": True}
    args, kwargs = run.call_args
    assert args[0] == ["gh", "api", f"repos/{module.CANARY_REPO}/pulls/3", "--method", "PATCH", "--input", "-"]
    assert json.loads(kwargs["input"]) == {"body": "release"}
    assert kwargs["timeout"] == 30
    assert kwargs["check"] is True
    assert module.kubectl("get", "nodes") == '{"ok":true}'
    assert run.call_args.args[0][:2] == ["kubectl", "--request-timeout=15s"]
    run.return_value.stdout = ""
    assert module.api("actions/runs/1/cancel", method="POST") == {}


def test_wait_convergence_and_deadline(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 1, 2, 3]))
    sleep = Mock()
    monkeypatch.setattr(module.time, "sleep", sleep)
    check = Mock(side_effect=[ValueError("not zero"), False, True])
    module.wait_for("drain", check, timeout=5)
    assert sleep.call_args_list == [call(4), call(3)]
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 2]))
    with pytest.raises(TimeoutError, match=r"deadline exceeded.*API unavailable"):
        module.wait_for("queued", Mock(side_effect=subprocess.SubprocessError("API unavailable")), timeout=1)
    monkeypatch.setattr(module.time, "monotonic", Mock(return_value=0))
    with pytest.raises(RuntimeError, match="escaped"):
        module.wait_for("queued", Mock(side_effect=RuntimeError("escaped")), timeout=1)


def test_prepare_and_recipe(controller, monkeypatch):
    command = Mock(return_value=subprocess.CompletedProcess([], 0, '{"cpu":8}', ""))
    monkeypatch.setattr(module, "command", command)
    controller.prepare()
    assert controller.desired == {"cpu": 8}
    assert command.call_args_list[0].args[0] == ["just", "kubeconfig", CLUSTER]
    assert command.call_args_list[1].args[0][-1] == CLUSTER
    assert command.call_args.kwargs["env"]["CLUSTERS_YAML"] == str(controller.args.clusters_yaml)


def test_wrong_group_preflight_is_read_only(controller, monkeypatch):
    controller.args.clusters_yaml.write_text(f"clusters:\n  {CLUSTER}:\n    arc-runners:\n      runner_group: other\n")
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match="Expected runner group"):
        controller.prepare()
    command.assert_not_called()


def test_scale_sets_and_live_metrics(controller, monkeypatch):
    pod = {
        "metadata": {"name": "listener", "labels": {SCALE_SET_LABEL: "cpu"}},
        "spec": {"containers": [{"ports": [{"name": "metrics", "containerPort": 8080}]}]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    linked = {
        "metadata": {"name": "listener"},
        "spec": {"autoscalingRunnerSetName": "cpu", "ephemeralRunnerSetName": "cpu-ers"},
    }
    kube = Mock(
        side_effect=[
            json.dumps({"items": ars()}),
            json.dumps({"items": [pod]}),
            json.dumps({"items": [linked]}),
            f'{CAPACITY_METRIC}{{name="cpu-ers"}} 0\n',
        ]
    )
    monkeypatch.setattr(module, "kubectl", kube)
    assert controller.scale_sets() == ars()
    assert controller.capacities() == {"cpu": 0}
    assert kube.call_args.args == ("get", "--raw", "/api/v1/namespaces/arc-systems/pods/listener:8080/proxy/metrics")


def test_unique_canary_pr_tree_and_branch(controller, monkeypatch):
    api = Mock(
        side_effect=[
            {"object": {"sha": "base"}},
            {"tree": {"sha": "base-tree"}},
            {"sha": "workflows"},
            {"sha": "tree"},
            {"sha": "commit"},
            {},
            {"number": 4},
        ]
    )
    monkeypatch.setattr(module, "api", api)
    controller.create_pr()
    assert controller.pr == 4
    assert controller.branch_created
    assert api.call_args_list[3].args[1] == {
        "base_tree": "base-tree",
        "tree": [{"path": ".github/workflows", "mode": "040000", "type": "tree", "sha": "workflows"}],
    }
    ref = api.call_args_list[5].args[1]
    assert ref == {"ref": f"refs/heads/{controller.branch}", "sha": "commit"}
    assert ref["ref"] != "refs/heads/main"
    assert "OSDC_DRAIN_RELEASE_abc123" not in api.call_args_list[6].args[1]["body"]


def test_ref_timeout_retains_cleanup_intent(controller, monkeypatch):
    api = Mock(
        side_effect=[
            {"object": {"sha": "base"}},
            {"tree": {"sha": "base-tree"}},
            {"sha": "workflows"},
            {"sha": "tree"},
            {"sha": "commit"},
            subprocess.TimeoutExpired("gh", 30),
        ]
    )
    monkeypatch.setattr(module, "api", api)
    with pytest.raises(subprocess.TimeoutExpired):
        controller.create_pr()
    assert controller.branch_created


def test_canary_run_lookup_and_job_api(controller, monkeypatch):
    api = Mock(
        return_value={"workflow_runs": [{"id": 99, "name": controller.workflow_name}, {"id": 100, "name": "unrelated"}]}
    )
    monkeypatch.setattr(module, "api", api)
    assert controller.find_run()
    assert controller.run_id == 99
    api.return_value = {"jobs": [job()]}
    assert controller.jobs() == [job()]
    assert api.call_args.args[0] == "actions/runs/99/jobs?per_page=100"
    api.return_value = {"workflow_runs": []}
    assert not controller.find_run()
    api.return_value = {"workflow_runs": [{"name": controller.workflow_name}] * 2}
    with pytest.raises(RuntimeError, match="Expected one canary run"):
        controller.find_run()


def test_wait_for_actual_inflight_step(controller, monkeypatch):
    running = job(status="in_progress")
    jobs = Mock(return_value=[running])
    monkeypatch.setattr(controller, "jobs", jobs)
    assert not controller.inflight_ready()
    running["steps"] = [{"name": "Wait for drain", "status": "in_progress"}]
    assert not controller.inflight_ready()
    running["runner_id"] = 3
    assert controller.inflight_ready()
    running["runner_group_name"] = "other"
    with pytest.raises(RuntimeError, match="different runner group"):
        controller.inflight_ready()
    jobs.return_value = [job()]
    with pytest.raises(RuntimeError, match="finished before drain"):
        controller.inflight_ready()


def test_drained_requires_config_and_capacity(controller, monkeypatch):
    sets = Mock(return_value=ars(0))
    caps = Mock(return_value={"cpu": 0})
    monkeypatch.setattr(controller, "scale_sets", sets)
    monkeypatch.setattr(controller, "capacities", caps)
    assert controller.drained()
    caps.return_value = {"cpu": 2147483647}
    with pytest.raises(ValueError, match="still advertise capacity"):
        controller.drained()
    sets.return_value = ars(8)
    with pytest.raises(ValueError, match="Runner limits differ"):
        controller.drained()


def test_completion_and_held_job_invariants(controller, monkeypatch):
    jobs = Mock(return_value=[job(status="queued")])
    monkeypatch.setattr(controller, "jobs", jobs)
    assert not controller.completed("inflight", 1)
    jobs.return_value = [job()]
    assert controller.completed("inflight", 1)
    jobs.return_value = [job(conclusion="failure")]
    with pytest.raises(RuntimeError, match="probe failed"):
        controller.completed("inflight", 1)
    jobs.return_value = [job(group="other")]
    with pytest.raises(RuntimeError, match="did not succeed"):
        controller.completed("inflight", 1)
    jobs.return_value = [job("held", status="queued")] * 3
    assert controller.held_ready()
    jobs.return_value = [job("held")] * 3
    with pytest.raises(RuntimeError, match="escaped"):
        controller.held_ready()


def test_observation_window_checks_every_poll(controller, monkeypatch):
    held = Mock(return_value=True)
    drained = Mock(return_value=True)
    monkeypatch.setattr(controller, "held_ready", held)
    monkeypatch.setattr(controller, "drained", drained)
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 1, 61]))
    sleep = Mock()
    monkeypatch.setattr(module.time, "sleep", sleep)
    controller.observe_held()
    assert held.call_count == drained.call_count == 2
    sleep.assert_called_once_with(10)
    drained.return_value = False
    monkeypatch.setattr(module.time, "monotonic", Mock(return_value=0))
    with pytest.raises(RuntimeError, match="remain in effect"):
        controller.observe_held()


def test_resume_verifies_limits_and_taints(controller, monkeypatch):
    controller.draining = True
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    sets = Mock(return_value=ars(8))
    monkeypatch.setattr(controller, "scale_sets", sets)
    kube = Mock(return_value='{"items":[]}')
    monkeypatch.setattr(module, "kubectl", kube)
    controller.resume()
    recipe.assert_called_once_with("resume-runners")
    assert not controller.draining
    controller.draining = True
    sets.return_value = ars(0)
    with pytest.raises(ValueError, match="Runner limits differ"):
        controller.resume()
    assert controller.draining
    sets.return_value = ars(8)
    kube.return_value = json.dumps({"items": [{"spec": {"taints": [{"key": "deploy.osdc.io/refresh-pending"}]}}]})
    with pytest.raises(ValueError, match="still have"):
        controller.resume()


def test_controller_orders_drain_release_observe_and_resume(controller, monkeypatch):
    events = []
    for name in ("prepare", "create_pr", "stop_drain", "observe_held"):
        monkeypatch.setattr(controller, name, lambda name=name: events.append(name))
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "capacities", lambda: {"cpu": 0})
    for name in ("find_run", "inflight_ready", "held_ready", "drained"):
        monkeypatch.setattr(controller, name, lambda name=name: events.append(name) or True)
    monkeypatch.setattr(controller, "completed", lambda phase, count: events.append(f"complete-{phase}") or True)
    monkeypatch.setattr(controller, "resume", lambda: events.append("resume"))
    monkeypatch.setattr(module, "wait_for", lambda description, check, timeout: check())
    api = Mock(side_effect=lambda *a: events.append("release") or {})
    monkeypatch.setattr(module, "api", api)
    proc = Mock()
    proc.wait.return_value = 0
    popen = Mock(return_value=proc)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    controller.run()
    assert events == [
        "prepare",
        "create_pr",
        "find_run",
        "inflight_ready",
        "drained",
        "release",
        "complete-inflight",
        "stop_drain",
        "held_ready",
        "observe_held",
        "resume",
        "complete-held",
    ]
    assert controller.draining
    popen.assert_called_once()
    assert popen.call_args.args[0] == ["just", "drain-runners", CLUSTER]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"]["OSDC_TAINT_NODES"] == "no"
    assert api.call_args.args[1] == {"body": "OSDC_DRAIN_RELEASE_abc123"}


def test_nonzero_recipe_exit_is_failure(controller, monkeypatch):
    for name in ("prepare", "create_pr"):
        monkeypatch.setattr(controller, name, Mock())
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "capacities", Mock())
    monkeypatch.setattr(module, "wait_for", Mock())
    monkeypatch.setattr(module, "api", Mock())
    proc = Mock()
    proc.wait.return_value = 1
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    with pytest.raises(RuntimeError, match="drain-runners failed"):
        controller.run()
    assert controller.draining


def test_drain_cannot_pass_without_zero_limits(controller, monkeypatch):
    for name in ("prepare", "create_pr", "find_run", "inflight_ready"):
        monkeypatch.setattr(controller, name, Mock(return_value=True))
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(8))
    monkeypatch.setattr(controller, "capacities", Mock(return_value={"cpu": 0}))
    popen = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    actual_wait = module.wait_for

    def wait(description, check, timeout):
        if description.startswith("zero runner"):
            return actual_wait(description, check, 0)
        return check()

    monkeypatch.setattr(module, "wait_for", wait)
    with pytest.raises(TimeoutError, match="Runner limits differ"):
        controller.run()
    popen.assert_called_once()


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ("", False),
        ("123 S\n456 R\n", True),
        ("123 Z+\n456 S\n", False),
        ("123 Z\n123 R+\n", True),
        ("456 S\n", False),
    ],
)
def test_process_group_running_ignores_other_groups_and_zombies(monkeypatch, snapshot, expected):
    command = Mock(return_value=subprocess.CompletedProcess([], 0, snapshot, ""))
    monkeypatch.setattr(module, "command", command)
    assert module.process_group_running(123) is expected
    command.assert_called_once_with(["ps", "-eo", "pgid=,stat="], timeout=2)


@pytest.mark.parametrize("force_kill", [False, True])
def test_stop_drain_kills_owned_process_group(controller, monkeypatch, force_kill):
    proc = Mock(pid=123)
    controller.drain = proc
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    running = Mock(side_effect=[True, False] if force_kill else [False])
    monkeypatch.setattr(module, "process_group_running", running)
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 3, 3] if force_kill else [0]))
    controller.stop_drain()
    assert kill.call_args_list[0] == call(123, signal.SIGTERM)
    assert kill.call_count == (2 if force_kill else 1)
    if force_kill:
        assert kill.call_args_list[-1] == call(123, signal.SIGKILL)
    proc.wait.assert_called_once_with(timeout=2)
    assert controller.drain is None
    controller.stop_drain()
    assert kill.call_count == (2 if force_kill else 1)


def test_stop_drain_exit_race(controller, monkeypatch):
    proc = Mock(pid=123)
    controller.drain = proc
    monkeypatch.setattr(module.os, "killpg", Mock(side_effect=ProcessLookupError))
    monkeypatch.setattr(module, "process_group_running", Mock(return_value=False))
    controller.stop_drain()
    proc.wait.assert_called_once_with(timeout=2)


def test_cleanup_refuses_resume_if_process_group_survives(controller, monkeypatch):
    controller.drain = Mock(pid=123)
    controller.draining = True
    kill = Mock()
    resume = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(module, "process_group_running", Mock(return_value=True))
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0, 3, 3, 6]))
    assert not controller.cleanup()
    assert kill.call_args_list == [call(123, signal.SIGTERM), call(123, signal.SIGKILL)]
    resume.assert_not_called()
    assert controller.draining
    assert controller.drain is not None


@pytest.mark.parametrize("leader_exited", [False, True])
def test_stop_drain_terminates_real_descendant_after_leader_exit(controller, leader_exited):
    # Only local owned processes: the child ignores TERM and must receive KILL.
    child = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(os.getpid(), flush=True); time.sleep(30)"
    )
    parent = (
        "import subprocess, sys; "
        "p = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdout=subprocess.PIPE, text=True); "
        "print(p.stdout.readline().strip(), flush=True); "
        "sys.exit(0) if sys.argv[2] == 'exit' else p.wait()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent, child, "exit" if leader_exited else "wait"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    controller.drain = proc
    try:
        child_pid = int(proc.stdout.readline())
        assert child_pid != proc.pid
        if leader_exited:
            proc.wait(timeout=5)
        assert module.process_group_running(proc.pid)
        controller.stop_drain()
        assert not module.process_group_running(proc.pid)
        assert proc.poll() is not None
        assert controller.drain is None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        proc.stdout.close()


def test_cleanup_restores_before_github_and_is_idempotent(controller, monkeypatch):
    events = []
    controller.draining = controller.branch_created = True

    def resume():
        events.append("resume")
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "runs", lambda: [{"id": 1, "status": "queued"}, {"id": 2, "status": "completed"}])
    api = Mock(side_effect=lambda *a, **kw: events.append(a[0]) or {})
    monkeypatch.setattr(module, "api", api)
    monkeypatch.setattr(module, "command", Mock(return_value=subprocess.CompletedProcess([], 0, '[{"number":3}]', "")))
    assert controller.cleanup()
    assert events == ["resume", "actions/runs/1/cancel", "pulls/3", f"git/refs/heads/{controller.branch}"]
    assert not controller.branch_created
    assert controller.cleanup()
    assert len(events) == 4


def test_cleanup_retries_restore_and_keeps_failed_state(controller, monkeypatch, caplog):
    controller.draining = True
    resume = Mock(side_effect=ValueError("not restored"))
    monkeypatch.setattr(controller, "resume", resume)
    assert not controller.cleanup()
    assert resume.call_count == 3
    assert controller.draining
    assert "RECOVERY REQUIRED" in caplog.text

    def recovered():
        controller.draining = False

    attempt = 0

    def transient_then_recovered():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise ValueError("transient")
        recovered()

    resume.side_effect = transient_then_recovered
    assert controller.cleanup()
    assert attempt == 2


def test_cleanup_errors_do_not_hide_restore(controller, monkeypatch):
    controller.draining = controller.branch_created = True

    def resume():
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "runs", Mock(side_effect=subprocess.SubprocessError("GitHub unavailable")))
    assert not controller.cleanup()
    assert not controller.draining
    assert controller.branch_created
    monkeypatch.setattr(controller, "stop_drain", Mock(side_effect=OSError("cannot stop")))
    assert not controller.cleanup()


def test_positive_and_cli_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER])
    args = module.parse_args()
    assert args.start_timeout == args.finish_timeout == 1800
    assert args.root_dir == module.UPSTREAM
    assert module.positive("3") == 3
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        module.positive("0")


@pytest.mark.parametrize(
    "extra",
    [
        ["--cluster-id", "meta-prod-aws-ue1"],
        ["--cluster-id", CLUSTER, "--start-timeout", "0"],
        ["--cluster-id", CLUSTER, "--dry-run", "--restore-only"],
    ],
)
def test_cli_rejects_unsafe_cluster_and_modes(monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["test", *extra])
    with pytest.raises(SystemExit):
        module.parse_args()


def test_signal_exits_with_original_status():
    with pytest.raises(SystemExit) as exc:
        module.on_signal(signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM


def test_dry_run_has_no_external_effects(controller, monkeypatch, capsys):
    controller.args.dry_run = True
    monkeypatch.setattr(module, "parse_args", lambda: controller.args)
    constructor = Mock()
    monkeypatch.setattr(module, "DrainTest", constructor)
    monkeypatch.setattr(module, "command", Mock(side_effect=AssertionError("external command")))
    module.main()
    assert "OSDC Drain Test" in capsys.readouterr().out
    constructor.assert_not_called()


@pytest.mark.parametrize("mode", ["run", "restore", "failure", "signal", "cleanup-failure"])
def test_main_always_cleans_up_and_restores_signal_handlers(controller, monkeypatch, mode):
    controller.args.restore_only = mode == "restore"
    monkeypatch.setattr(module, "parse_args", lambda: controller.args)
    monkeypatch.setattr(module, "DrainTest", lambda *a: controller)
    for name in ("prepare", "resume", "run"):
        monkeypatch.setattr(controller, name, Mock())
    if mode == "failure":
        controller.run.side_effect = RuntimeError("test failed")
    if mode == "signal":
        controller.run.side_effect = SystemExit(130)
    cleanup = Mock(return_value=mode != "cleanup-failure")
    monkeypatch.setattr(controller, "cleanup", cleanup)
    signals = Mock(return_value="previous")
    monkeypatch.setattr(module.signal, "signal", signals)
    register = Mock()
    unregister = Mock()
    monkeypatch.setattr(module.atexit, "register", register)
    monkeypatch.setattr(module.atexit, "unregister", unregister)
    if mode in {"failure", "cleanup-failure"}:
        with pytest.raises(RuntimeError, match=r"test failed|cleanup failed"):
            module.main()
    elif mode == "signal":
        with pytest.raises(SystemExit) as exc:
            module.main()
        assert exc.value.code == 130
    else:
        module.main()
    cleanup.assert_called_once()
    register.assert_called_once_with(cleanup)
    assert unregister.call_count == (0 if mode == "cleanup-failure" else 1)
    assert call(signal.SIGTERM, signal.SIG_IGN) in signals.call_args_list
    assert call(signal.SIGTERM, "previous") in signals.call_args_list
    if mode == "restore":
        controller.prepare.assert_called_once()
        controller.resume.assert_called_once()


def test_group_pinning_and_release_gate():
    content = module.generate_workflow("abc123")
    workflow = yaml.safe_load(content)
    assert workflow["name"] == "OSDC Drain Test abc123"
    assert workflow[True]["pull_request"]["types"] == ["opened"]
    # Editing the PR body releases the probe; it must not create another run.
    for job in workflow["jobs"].values():
        assert job["runs-on"] == {"group": CLUSTER, "labels": [PROBE_LABEL]}
        assert int(job["timeout-minutes"]) > 0
    inflight = workflow["jobs"]["drain-inflight"]
    assert inflight["steps"][0]["env"]["RELEASE_MARKER"] == "OSDC_DRAIN_RELEASE_abc123"
    held = workflow["jobs"]["drain-held"]
    assert held["needs"] == "drain-inflight"
    assert len(held["strategy"]["matrix"]["probe"]) == 3
    assert "time.monotonic()" in inflight["steps"][0]["run"]


@pytest.mark.parametrize("key", ["", "../bad", "newline\n", "bad-key"])
def test_invalid_keys(key):
    with pytest.raises(ValueError, match="Run key"):
        module.generate_workflow(key)


@pytest.mark.parametrize("transient", [None, "network", 503, "incomplete", "timeout", "reset"])
def test_probe_wait_code_releases_and_retries(monkeypatch, transient):
    workflow = yaml.safe_load(module.generate_workflow("abc123"))
    step = workflow["jobs"]["drain-inflight"]["steps"][0]
    monkeypatch.setenv("PR_URL", "https://api.github.com/repos/pytorch/pytorch-canary/pulls/1")
    monkeypatch.setenv("GH_TOKEN", "test")
    monkeypatch.setenv("RELEASE_MARKER", "OSDC_DRAIN_RELEASE_abc123")
    responses = [io.StringIO(json.dumps({"body": "OSDC_DRAIN_RELEASE_abc123"}))]
    if transient == "network":
        responses.insert(0, urllib.error.URLError("offline"))
    elif transient in ("incomplete", "timeout", "reset"):
        error = {
            "incomplete": http.client.IncompleteRead(b'{"body":', 20),
            "timeout": TimeoutError("Response body timed out"),
            "reset": ConnectionResetError("Connection reset while reading body"),
        }[transient]

        class InterruptedResponse(io.StringIO):
            def read(self, *args):
                raise error

        responses.insert(0, InterruptedResponse())
    elif transient is not None:
        responses.insert(0, urllib.error.HTTPError("https://api.github.com", transient, "retry", {}, None))
    read = Mock(side_effect=responses)
    monkeypatch.setattr(urllib.request, "urlopen", read)
    monkeypatch.setattr(time, "monotonic", Mock(return_value=0))
    monkeypatch.setattr(time, "sleep", Mock())
    # Execute only our local template, with its external effects mocked.
    exec(compile(step["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0], "<probe>", "exec"), {})  # noqa: S102
    assert read.call_count == (1 if transient is None else 2)
    assert read.call_args.kwargs["timeout"] == 10


def test_probe_wait_does_not_retry_auth_failures(monkeypatch):
    workflow = yaml.safe_load(module.generate_workflow("abc123"))
    step = workflow["jobs"]["drain-inflight"]["steps"][0]
    monkeypatch.setenv("PR_URL", "https://api.github.com/repos/pytorch/pytorch-canary/pulls/1")
    monkeypatch.setenv("GH_TOKEN", "test")
    monkeypatch.setattr(time, "monotonic", Mock(return_value=0))
    sleep = Mock()
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        Mock(side_effect=urllib.error.HTTPError("https://api.github.com", 403, "forbidden", {}, None)),
    )
    with pytest.raises(urllib.error.HTTPError, match="403"):
        exec(compile(step["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0], "<probe>", "exec"), {})  # noqa: S102
    sleep.assert_not_called()


def test_probe_wait_code_has_its_own_deadline(monkeypatch):
    workflow = yaml.safe_load(module.generate_workflow("abc123"))
    code = workflow["jobs"]["drain-inflight"]["steps"][0]["run"].split("\n", 1)[1].rsplit("\nPY", 1)[0]
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 2401]))
    with pytest.raises(SystemExit, match="deadline"):
        exec(compile(code, "<probe>", "exec"), {})  # noqa: S102 -- local template, mocked clock/network


def test_ci_waits_for_all_other_slow_tests_and_reserves_recovery_time():
    workflow = yaml.safe_load((module.UPSTREAM.parent / ".github/workflows/_osdc-slow-tests.yml").read_text())
    test = workflow["jobs"]["drain-test"]
    assert set(test["needs"]) == set(workflow["jobs"]) - {"drain-test"}
    steps = {step.get("id", step.get("name")): step for step in test["steps"]}
    recovery = steps["Resume and verify staging runners"]
    assert "always()" in recovery["if"]
    assert "--restore-only" in recovery["run"]
    assert steps["drain"]["timeout-minutes"] + recovery["timeout-minutes"] < test["timeout-minutes"]
