"""Offline controller tests with mocked services and local process-cleanup regression checks."""

import argparse
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Event, Lock
from unittest.mock import ANY, Mock, call

import drain_test_run as module
import pytest
import yaml
from drain_test_checks import CAPACITY_METRIC, SCALE_SET_LABEL
from runner_max_map import build_max_runners_map, compute_ars_name

CLUSTER = "meta-staging-aws-uw1"
ORG_URL = "https://github.com/pytorch"
TEST_PROBE_LABEL = "test-prefix-l-x86iamx-8-16"


def canary_run(run_id, status):
    return {"id": run_id, "status": status, "name": "OSDC Drain Test previous123"}


def canary_pr(number):
    return {"number": number, "title": f"{module.PR_TITLE_PREFIX} previous123"}


@pytest.mark.parametrize("suffix", ["abc123", "Previous123", "a" * 32])
def test_canary_identity_accepts_current_and_previous_ascii_keys(suffix):
    assert module.is_canary_run({"name": f"{module.WORKFLOW_NAME_PREFIX}{suffix}"})
    module.validate_canary_resources([], [{"title": f"{module.PR_TITLE_PREFIX} {suffix}"}])


@pytest.fixture
def ci_environment(monkeypatch):
    values = {
        "OSDC_DRAIN_TEST_LIVE": "true",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_controller_stores_only_environment_overrides(controller, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-aws-secret")
    test = module.DrainTest(controller.args, "env123")
    assert test.env_overrides == {
        "OSDC_ROOT": str(controller.args.root_dir),
        "OSDC_UPSTREAM": str(controller.args.upstream_dir),
        "CLUSTERS_YAML": str(controller.args.root_dir / "clusters.yaml"),
        "OSDC_TAINT_NODES": "no",
        "OSDC_UNTAINT_NODES": "no",
    }


@pytest.mark.parametrize("recipe", ["kubeconfig", "resume-runners"])
def test_recipe_inherits_current_environment_and_applies_overrides(controller, monkeypatch, recipe):
    # The controller already exists: inherit changes made before each launch.
    inherited = {
        "PATH": "/test/bin",
        "AWS_SESSION_TOKEN": "test-session-token",
        "HTTPS_PROXY": "http://proxy.test:8080",
        "KUBECONFIG": "/test/kubeconfig",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    for name in controller.env_overrides:
        monkeypatch.setenv(name, "ambient-value")
    monkeypatch.setenv("OSDC_TEST_ENV_REMOVED", "old-value")
    command = Mock(return_value=Mock(stdout="recipe output"))
    monkeypatch.setattr(module, "command", command)

    controller.recipe(recipe)
    first_env = command.call_args.kwargs["env"]
    for name, value in inherited.items():
        assert first_env[name] == value
    for name, value in controller.env_overrides.items():
        assert first_env[name] == value
        assert os.environ[name] == "ambient-value"

    monkeypatch.setenv("AWS_SESSION_TOKEN", "rotated-session-token")
    monkeypatch.delenv("OSDC_TEST_ENV_REMOVED")
    controller.recipe(recipe)
    second_env = command.call_args.kwargs["env"]
    assert second_env["AWS_SESSION_TOKEN"] == os.environ["AWS_SESSION_TOKEN"]
    assert "OSDC_TEST_ENV_REMOVED" not in second_env
    assert first_env["AWS_SESSION_TOKEN"] == inherited["AWS_SESSION_TOKEN"]
    assert "AWS_SESSION_TOKEN" not in controller.env_overrides


def gated_job(number, *, status="in_progress"):
    result = job("held", status=status, job_id=100 + number)
    result.update(
        name=f"drain-held ({number})",
        runner_id=200 + number if status != "queued" else 0,
        runner_name=f"probe-{number}" if status != "queued" else "",
        steps=[{"name": module.HELD_RELEASE_STEP, "status": "in_progress"}],
    )
    return result


@pytest.fixture
def placement_controller(controller, monkeypatch):
    controller.pr = 42
    jobs = [gated_job(number) for number in (1, 2, 3)]
    pods, nodes = [], [refresh_node("stale")]
    for number in (1, 2, 3):
        for role in ("runner", "workflow"):
            name = f"probe-{number}" if role == "runner" else f"probe-{number}-workflow"
            labels = (
                {SCALE_SET_LABEL: "cpu", "app.kubernetes.io/component": "runner"}
                if role == "runner"
                else {"runner-pod": f"probe-{number}"}
            )
            pods.append(
                {
                    "metadata": {"name": name, "uid": f"{name}-uid", "labels": labels},
                    "spec": {
                        "nodeName": f"{role}-node",
                        "containers": [{"name": "job" if role == "workflow" else "runner"}],
                    },
                    "status": {"phase": "Running"},
                }
            )
    nodes += [
        {"metadata": {"name": f"{role}-node", "uid": f"{role}-node-uid"}, "spec": {}} for role in ("runner", "workflow")
    ]

    def kube(*args):
        assert args[0] == "get", "Placement checks and cleanup must never mutate node taints"
        return json.dumps({"items": pods if args[1] == "pods" else nodes})

    controller.refresh_nodes = {"stale": "runner"}
    controller.probe_ids["held"] = {item["id"] for item in jobs}
    monkeypatch.setattr(controller, "jobs", lambda: jobs)
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "kube", Mock(side_effect=kube))
    monkeypatch.setattr(controller, "api", Mock(return_value={}))
    return controller, jobs, pods, nodes


def test_held_probes_are_verified_and_released_individually(placement_controller):
    test, jobs, pods, nodes = placement_controller
    jobs[1:] = [gated_job(number, status="queued") for number in (2, 3)]
    for number in (1, 2, 3):
        jobs[number - 1] = gated_job(number)
        assert not test.held_completed()
        assert set(test.placements) == test.released_probes == set(range(1, number + 1))
        call = test.api.call_args
        assert call.args == (
            "pulls/42",
            {
                "body": "\n".join(
                    ["OSDC_DRAIN_RELEASE_abc123", *[f"OSDC_HELD_RELEASE_abc123_{n}" for n in range(1, number + 1)]]
                )
            },
            "PATCH",
        )
        evidence = test.placements[number]
        assert evidence["job_id"] == jobs[number - 1]["id"]
        assert evidence["runner"]["node_uid"] == "runner-node-uid"
        assert evidence["workflow"]["node_uid"] == "workflow-node-uid"
        jobs[number - 1]["status"] = "completed"
        pods[:] = [pod for pod in pods if not pod["metadata"]["name"].startswith(f"probe-{number}")]
    # Completed probes can be checked after their pods disappear.
    assert test.held_completed()
    assert test.api.call_count == 3
    assert test.has_refresh_taint(nodes[0])


@pytest.mark.parametrize("failure", ["runner-node", "workflow-node", "taint-removed"])
def test_placement_failure_never_releases_probe(placement_controller, failure):
    test, _jobs, _pods, nodes = placement_controller
    if failure == "taint-removed":
        nodes[0]["spec"]["taints"] = []
    else:
        test.refresh_nodes[nodes[1 if failure == "runner-node" else 2]["metadata"]["uid"]] = failure
        nodes[1 if failure == "runner-node" else 2]["spec"] = nodes[0]["spec"]
    with pytest.raises(RuntimeError, match=r"reused stale node|Refresh taint removed"):
        test.held_completed()
    test.api.assert_not_called()


@pytest.mark.parametrize("failure", ["missing-pod", "ambiguous-workflow", "api"])
def test_missing_placement_evidence_times_out_without_release(placement_controller, monkeypatch, clock, failure):
    test, _jobs, pods, _nodes = placement_controller
    if failure == "missing-pod":
        pods.pop(0)
    elif failure == "ambiguous-workflow":
        pods.append(pods[1])
    else:
        original_kube = test.kube.side_effect

        def kube(*args):
            if args[1] == "pods":
                raise subprocess.SubprocessError("Pod API unavailable")
            return original_kube(*args)

        test.kube.side_effect = kube
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        test.wait_for("placement evidence", test.held_completed, 10, poll=5)
    test.api.assert_not_called()
    assert not test.released_probes
    if failure == "api":
        pod_calls = [c for c in test.kube.call_args_list if c.args[1] == "pods"]
        assert len(pod_calls) == 2
        assert clock[0] == 10


def test_held_jobs_wait_for_gate_and_can_complete_with_empty_stale_baseline(placement_controller):
    test, jobs, _pods, _nodes = placement_controller
    test.refresh_nodes.clear()
    for item in jobs:
        item["steps"] = []
    assert not test.held_completed()
    test.api.assert_not_called()
    test.kube.assert_not_called()
    for item in jobs:
        item["steps"] = [{"name": module.HELD_RELEASE_STEP, "status": "in_progress"}]
    assert not test.held_completed()
    assert len(test.placements) == 3
    for item in jobs:
        item["status"] = "completed"
    assert test.held_completed()


@pytest.mark.parametrize("failed_probe", [1, 3])
@pytest.mark.parametrize("applied", [False, True])
def test_held_release_retries_same_body_after_uncertain_write(placement_controller, failed_probe, applied):
    test, jobs, pods, _nodes = placement_controller

    def release(endpoint, payload, method):
        failing = test.api.call_count == failed_probe
        if applied or not failing:
            for number, item in enumerate(jobs, 1):
                if f"OSDC_HELD_RELEASE_abc123_{number}" in payload["body"].splitlines():
                    item["status"] = "completed"
        if failing:
            raise subprocess.SubprocessError("PATCH failed")
        return {}

    test.api.side_effect = release
    with pytest.raises(subprocess.SubprocessError, match="PATCH failed"):
        test.held_completed()
    pending = test.pending_held_release
    assert pending is not None
    assert set(test.placements) == test.released_probes == set(range(1, failed_probe + 1))
    completed_names = {item["runner_name"] for item in jobs if item["status"] == "completed"}
    pods[:] = [pod for pod in pods if pod["metadata"]["name"].removesuffix("-workflow") not in completed_names]

    assert test.held_completed()
    assert test.api.call_args_list[failed_probe] == test.api.call_args_list[failed_probe - 1]
    assert test.api.call_args_list[failed_probe].args == ("pulls/42", {"body": pending}, "PATCH")
    assert test.pending_held_release is None
    assert test.released_probes == {1, 2, 3}
    assert test.api.call_count == 4
    assert test.held_completed()
    assert test.api.call_count == 4  # Confirmed releases do not need another PATCH.


@pytest.mark.parametrize(("budget", "elapsed"), [(7, 7), (60, 20)])
def test_held_release_retries_stop_at_completion_or_controller_deadline(placement_controller, clock, budget, elapsed):
    test, _jobs, _pods, _nodes = placement_controller
    test.api.side_effect = [{}, {}, subprocess.SubprocessError("PATCH failed")]
    with pytest.raises(subprocess.SubprocessError, match="PATCH failed"):
        test.held_completed()
    pending = test.pending_held_release
    test.api.reset_mock(side_effect=True)
    test.api.side_effect = subprocess.SubprocessError("PATCH unavailable")
    with pytest.raises(TimeoutError, match="held release"), test.deadline_scope(budget):
        test.wait_for("held release", test.held_completed, 20, poll=5)
    assert clock[0] == elapsed
    assert test.pending_held_release == pending
    patch = call("pulls/42", {"body": pending}, "PATCH")
    assert test.api.call_args_list == [patch] * test.api.call_count
    assert test.api.call_count > 1


def test_held_completed_tolerates_released_probe_job_disappearance(placement_controller):
    test, jobs, pods, _nodes = placement_controller
    # Release probe 1, then lose the PATCH response for probe 2 after the server applied it.
    test.api.side_effect = [{}, subprocess.SubprocessError("PATCH response lost"), {}, {}]
    with pytest.raises(subprocess.SubprocessError, match="response lost"):
        test.held_completed()
    assert set(test.placements) == {1, 2}
    assert test.released_probes == {1, 2}
    # Probe 1 completes and disappears from the GitHub jobs API.
    jobs[0]["status"] = "completed"
    jobs.pop(0)
    # Probe 2 was also released by the server despite the lost response.
    jobs[0]["status"] = "completed"
    pods[:] = [pod for pod in pods if not pod["metadata"]["name"].startswith("probe-1")]
    assert not test.held_completed()  # verifies and releases probe 3, which is still running
    jobs[1]["status"] = "completed"
    assert test.held_completed()
    assert test.released_probes == {1, 2, 3}


def test_placement_wait_allows_wakeup_and_single_slot_progress(placement_controller, monkeypatch, clock):
    test, jobs, _pods, _nodes = placement_controller
    jobs[:] = [gated_job(number, status="queued") for number in (1, 2, 3)]
    monkeypatch.setattr(test, "capacities", lambda expected=None: {"cpu": 8})
    wakeup = Mock(side_effect=lambda *args: jobs.__setitem__(0, gated_job(1)))
    monkeypatch.setattr(test, "wakeup", wakeup)

    def release(path, body, method):
        assert path == "pulls/42"
        assert method == "PATCH"
        for number in (1, 2, 3):
            if f"OSDC_HELD_RELEASE_abc123_{number}" in body["body"].splitlines():
                jobs[number - 1]["status"] = "completed"
                if number < 3 and jobs[number]["status"] == "queued":
                    jobs[number] = gated_job(number + 1)

    test.api.side_effect = release
    test.wait_with_wakeup("held placement and completion", "held", test.held_completed, 30)
    assert test.released_probes == {1, 2, 3}
    assert all(item["status"] == "completed" for item in jobs)
    wakeup.assert_called_once_with("held", 1, 30)


@pytest.mark.parametrize("failure", ["job-id", "runner-id", "runner-name", "swapped-job-ids"])
def test_original_probe_identity_cannot_change_after_verification(placement_controller, failure):
    test, jobs, _pods, _nodes = placement_controller
    assert not test.held_completed()
    if failure == "swapped-job-ids":
        jobs[0]["id"], jobs[1]["id"] = jobs[1]["id"], jobs[0]["id"]
    else:
        jobs[0][{"job-id": "id", "runner-id": "runner_id", "runner-name": "runner_name"}[failure]] = "replacement"
    with pytest.raises(RuntimeError, match=r"job IDs|jobs changed|changed runner"):
        test.held_completed()
    assert test.api.call_count == 3


@pytest.mark.parametrize("group", [None, ""])
def test_held_missing_runner_group_is_tolerated_while_awaiting_placement(placement_controller, group):
    test, jobs, _pods, _nodes = placement_controller
    jobs[0]["runner_group_name"] = group
    assert not test.held_completed()
    assert 1 in test.placements
    test.api.assert_called()


@pytest.mark.parametrize(
    "failure", ["wrong-group", "failed", "completed-without-runner", "completed-before-gate", "duplicate-probe"]
)
def test_invalid_held_results_fail_before_release(placement_controller, failure):
    test, jobs, _pods, _nodes = placement_controller
    if failure == "wrong-group":
        jobs[0]["runner_group_name"] = "other-cluster"
    elif failure == "failed":
        jobs[0].update(status="completed", conclusion="failure")
    elif failure == "completed-without-runner":
        jobs[0].update(status="completed", runner_id=0)
    elif failure == "completed-before-gate":
        jobs[0]["status"] = "completed"
    else:
        jobs[0]["name"] = jobs[1]["name"]
    with pytest.raises(RuntimeError):
        test.held_completed()
    test.api.assert_not_called()


@pytest.mark.parametrize("mode", ["success", "failure", "ci-failure", "restore-only"])
def test_cleanup_and_recovery_preserve_all_taints_without_waiting_for_probes(
    placement_controller, monkeypatch, controller_runtime, mode
):
    test, _jobs, _pods, nodes = placement_controller
    added = refresh_node("test-added")
    added["metadata"]["name"] = "test-added-node"
    nodes.append(added)
    before = json.dumps(nodes)
    test.draining = True
    monkeypatch.setattr(test, "runner_nodes", lambda: nodes)
    monkeypatch.setattr(test, "scale_sets", lambda: ars())
    recipe = Mock()
    monkeypatch.setattr(test, "recipe", recipe)
    monkeypatch.setattr(test, "prepare", Mock())
    probes = Mock(side_effect=AssertionError("Recovery must not wait for probe placement"))
    monkeypatch.setattr(test, "held_completed", probes)
    if mode in ("failure", "ci-failure"):
        test.defer_recovery = mode == "ci-failure"
        monkeypatch.setattr(test, "run", Mock(side_effect=RuntimeError("probe verification failed")))
        with pytest.raises(RuntimeError, match="probe verification failed"):
            module.run_controller(test)
        if mode == "ci-failure":
            recipe.assert_not_called()
            test.defer_recovery = test.skip_canary_cleanup = False
            module.run_controller(test, restore_only=True)
    elif mode == "restore-only":
        module.run_controller(test, restore_only=True)
    else:
        assert test.cleanup()
    assert not test.draining
    assert json.dumps(nodes) == before
    assert set(test.refresh_nodes) == {"stale", "test-added"}
    recipe.assert_called_once_with("resume-runners", timeout=210)
    probes.assert_not_called()


def test_partial_taint_failure_does_not_write_ownership_or_remove_taints(controller, monkeypatch, recovery_state):
    before = controller.recovery_file.read_text()
    node = refresh_node(tainted=False)
    monkeypatch.setattr(controller, "runner_nodes", lambda: [node])

    def recipe(name):
        node["spec"]["taints"] = refresh_node()["spec"]["taints"]
        raise RuntimeError("partial taint failure")

    monkeypatch.setattr(controller, "recipe", recipe)
    with pytest.raises(RuntimeError, match="partial taint failure"):
        controller.taint_runner_nodes()
    assert controller.recovery_file.read_text() == before
    assert controller.has_refresh_taint(node)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    config = tmp_path / "clusters.yaml"
    config.write_text(
        f"clusters:\n  {CLUSTER}:\n    arc-runners:\n"
        f"      runner_group: {CLUSTER}\n"
        f"      github_config_url: {ORG_URL}\n"
    )
    args = argparse.Namespace(
        cluster_id=CLUSTER,
        root_dir=tmp_path,
        upstream_dir=tmp_path,
        dry_run=False,
        restore_only=False,
        defer_recovery=False,
        recovery_file=None,
        run_timeout=module.RUN_TIMEOUT,
        cleanup_timeout=module.CLEANUP_TIMEOUT,
        restore_timeout=module.RESTORE_TIMEOUT,
        start_timeout=1800,
        finish_timeout=1800,
        drain_timeout=600,
    )
    test = module.DrainTest(args, "abc123")
    monkeypatch.setattr(test, "cleanup_stale_canary", Mock())
    test.group = CLUSTER
    test.desired = {"cpu": 8}
    test.probe_label = TEST_PROBE_LABEL
    return test


def ars(limit=8):
    return [{"metadata": {"name": "cpu"}, "spec": {"maxRunners": limit, "runnerScaleSetName": TEST_PROBE_LABEL}}]


def job(phase="inflight", status="completed", conclusion="success", group=CLUSTER, job_id=1):
    return {
        "id": job_id,
        "name": f"drain-{phase}",
        "status": status,
        "conclusion": conclusion,
        "runner_group_name": group,
    }


def test_command_and_api_transport(monkeypatch):
    proc = Mock(returncode=0)
    proc.communicate.return_value = ('{"ok":true}', "")
    run = Mock(return_value=proc)
    monkeypatch.setattr(module.subprocess, "Popen", run)
    assert module.api("pulls/3", {"body": "release"}, "PATCH") == {"ok": True}
    args, kwargs = run.call_args
    assert args[0] == ["gh", "api", f"repos/{module.CANARY_REPO}/pulls/3", "--method", "PATCH", "--input", "-"]
    assert json.loads(proc.communicate.call_args.kwargs["input"]) == {"body": "release"}
    assert proc.communicate.call_args.kwargs["timeout"] == 30
    assert kwargs["start_new_session"] is True
    assert module.kubectl("get", "nodes") == '{"ok":true}'
    assert run.call_args.args[0][:2] == ["kubectl", "--request-timeout=15s"]
    proc.communicate.return_value = ("", "")
    assert module.api("actions/runs/1/cancel", method="POST") == {}
    proc.returncode = 1
    result = module.command(["false"], check=False)
    assert result.returncode == 1
    assert result.stdout == result.stderr == ""
    assert "check" not in run.call_args.kwargs
    with pytest.raises(subprocess.CalledProcessError):
        module.command(["false"])


def test_cancelled_command_does_not_launch(monkeypatch):
    cancelled = Event()
    cancelled.set()
    launch = Mock()
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    with pytest.raises(CancelledError):
        module.command(["kubectl"], cancel_event=cancelled)
    launch.assert_not_called()


def test_command_cancellation_during_launch_kills_only_owned_group(monkeypatch):
    cancelled = Event()
    proc = Mock(pid=123)
    proc.communicate.return_value = ("", "")

    def launch(*args, **kwargs):
        cancelled.set()
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(CancelledError):
        module.command(["kubectl"], cancel_event=cancelled)
    kill.assert_called_once_with(123, signal.SIGKILL)


def test_cancellable_command_preserves_input_and_output_across_polls(monkeypatch):
    proc = Mock(returncode=0)
    proc.communicate.side_effect = [subprocess.TimeoutExpired("command", 0.1), ("output", "errors")]
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    result = module.command(["command"], input="payload", cancel_event=Event())
    assert [c.kwargs["input"] for c in proc.communicate.call_args_list] == ["payload", None]
    assert all(0 < c.kwargs["timeout"] <= 0.1 for c in proc.communicate.call_args_list)
    assert (result.stdout, result.stderr) == ("output", "errors")


@pytest.mark.parametrize(("deadline", "expected"), [(None, 20), (10, 10), (0, 0)])
def test_controller_api_uses_default_or_explicit_deadline(controller, monkeypatch, deadline, expected):
    controller.deadline = 20
    api = Mock(return_value={"ok": True})
    monkeypatch.setattr(module, "api", api)

    assert controller.api("issues/1", {"state": "closed"}, "PATCH", deadline=deadline, timeout=5) == {"ok": True}
    api.assert_called_once_with("issues/1", {"state": "closed"}, "PATCH", deadline=expected, timeout=5)
    assert controller.deadline == 20


def test_kubectl_forwards_cancellation_to_command(controller, monkeypatch):
    cancelled = Event()
    command = Mock(return_value=subprocess.CompletedProcess([], 0, "metrics", ""))
    monkeypatch.setattr(module, "command", command)
    assert controller.kube("get", "--raw", "/metrics", deadline=10, cancel_event=cancelled) == "metrics"
    command.assert_called_once_with(
        ["kubectl", "--request-timeout=15s", "get", "--raw", "/metrics"], deadline=10, cancel_event=cancelled
    )


@pytest.mark.parametrize("deadline", [None, 0.2, 0.15])
def test_cancellable_command_polls_share_one_timeout(monkeypatch, clock, deadline):
    proc = Mock(pid=123)
    kill = Mock()

    def communicate(**kwargs):
        if kill.called:
            return "", ""
        clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired("command", kwargs["timeout"], output="partial", stderr="errors")

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        module.command(["command"], timeout=0.2, deadline=deadline, cancel_event=Event())
    expected = min(0.2, deadline) if deadline is not None else 0.2
    assert exc.value.timeout == expected
    assert exc.value.output == "partial"
    assert exc.value.stderr == "errors"
    assert clock[0] == pytest.approx(expected)
    kill.assert_called_once_with(123, signal.SIGKILL)


@pytest.mark.parametrize("cancellable", [False, True])
@pytest.mark.parametrize(("returncode", "check"), [(1, True), (1, False), (0, True)])
def test_command_failure_diagnostics(caplog, returncode, check, cancellable):
    args = [sys.executable, "-"]
    script = (
        'import sys; print("cpu — FAILED: Forbidden"); '
        f'print("credentials expired", file=sys.stderr); sys.exit({returncode})'
    )
    if check and returncode != 0:
        with pytest.raises(subprocess.CalledProcessError) as exc:
            module.command(args, input=script, cancel_event=Event() if cancellable else None)
        assert exc.value.returncode == returncode
        assert exc.value.cmd == args
        assert exc.value.stdout == "cpu — FAILED: Forbidden\n"
        assert exc.value.stderr == "credentials expired\n"
        assert caplog.record_tuples == [
            (
                module.log.name,
                30,
                f"{args[0]} exited 1\nstdout: cpu — FAILED: Forbidden\nstderr: credentials expired",
            )
        ]
    else:
        result = module.command(args, input=script, check=check, cancel_event=Event() if cancellable else None)
        assert result.returncode == returncode
        assert result.stdout == "cpu — FAILED: Forbidden\n"
        assert result.stderr == "credentials expired\n"
        assert not caplog.records


@pytest.mark.parametrize("cancellable", [False, True])
@pytest.mark.parametrize(
    ("deadline", "finished", "check"),
    [(10.0, 9.0, True), (10.0, 10.0, False), (10.0, 12.0, True), (None, 12.0, False)],
)
def test_command_checks_deadline_before_return(monkeypatch, clock, cancellable, check, deadline, finished):
    proc = Mock(returncode=0)

    def communicate(**kwargs):
        clock[0] = finished
        return "output", "errors"

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    options = {"deadline": deadline, "check": check, "cancel_event": Event() if cancellable else None}

    if deadline is not None and finished >= deadline:
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            module.command(["command"], **options)
    else:
        result = module.command(["command"], **options)
        assert (result.returncode, result.stdout, result.stderr) == (0, "output", "errors")


@pytest.mark.parametrize("transport", ["api-get", "api-patch", "kubectl", "recipe"])
def test_transports_preserve_command_deadline(controller, monkeypatch, clock, transport):
    # command() owns the clock-boundary matrix. Each wrapper only needs to prove
    # it forwards the budget, including the controller API's omitted deadline.
    proc = Mock(returncode=0)

    def communicate(**kwargs):
        clock[0] = 12.0
        return '{"ok":true}', ""

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    controller.deadline = 10.0
    invoke = {
        "api-get": lambda: controller.api("foo/bar", method="GET"),
        "api-patch": lambda: controller.api("foo/bar", method="PATCH"),
        "kubectl": lambda: module.kubectl("get", "nodes", deadline=10.0),
        "recipe": lambda: controller.recipe("drain-runners"),
    }[transport]
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        invoke()


def test_wait_convergence_and_deadline(controller, clock):
    check = Mock(side_effect=[ValueError("not zero"), False, True])
    controller.wait_for("drain", check, timeout=5, poll=1)
    assert clock[0] == 2
    with pytest.raises(TimeoutError, match=r"deadline exceeded.*API unavailable"):
        controller.wait_for("queued", Mock(side_effect=subprocess.SubprocessError("API unavailable")), timeout=1)
    assert clock[0] == 3
    with pytest.raises(RuntimeError, match="escaped"):
        controller.wait_for("queued", Mock(side_effect=RuntimeError("escaped")), timeout=1)


@pytest.mark.parametrize(
    ("cluster_config", "defaults"),
    [
        pytest.param({"arc-runners": {"runner_group": CLUSTER, "github_config_url": ORG_URL}}, {}, id="cluster-block"),
        pytest.param(
            {"arc-runners": {"runner_group": CLUSTER, "github_config_url": ORG_URL}},
            {"arc-runners": {"runner_group": "other", "github_config_url": f"{ORG_URL}/pytorch-canary"}},
            id="cluster-block-overrides-defaults",
        ),
        pytest.param({}, {"arc-runners": {"runner_group": CLUSTER, "github_config_url": ORG_URL}}, id="defaults-block"),
        pytest.param(
            {"arc-runners": None},
            {"arc-runners": {"runner_group": CLUSTER, "github_config_url": ORG_URL}},
            id="null-block-fallback",
        ),
    ],
)
def test_prepare_and_recipe(controller, monkeypatch, cluster_config, defaults):
    monkeypatch.setenv("AWS_SESSION_TOKEN", "prepare-session-token")
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump({"clusters": {CLUSTER: cluster_config}, "defaults": defaults})
    )
    command = Mock(return_value=subprocess.CompletedProcess([], 0, '{"cpu":8}', ""))
    monkeypatch.setattr(module, "command", command)
    controller.prepare()
    assert controller.desired == {"cpu": 8}
    assert command.call_args_list[0].args[0] == ["just", "kubeconfig", CLUSTER]
    assert command.call_args_list[1].args[0][-1] == CLUSTER
    assert command.call_args.kwargs["env"]["CLUSTERS_YAML"] == str(controller.args.root_dir / "clusters.yaml")
    assert all(c.kwargs["env"]["AWS_SESSION_TOKEN"] == os.environ["AWS_SESSION_TOKEN"] for c in command.call_args_list)


def test_prepare_uses_root_config_for_probes_and_real_max_map(controller, monkeypatch):
    root = controller.args.root_dir
    controller.args.upstream_dir = module.UPSTREAM
    (root / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "clusters": {
                    CLUSTER: {
                        "modules": ["arc-runners"],
                        "arc-runners": {
                            "runner_group": "root-runners",
                            "runner_name_prefix": "root-",
                            "github_config_url": ORG_URL,
                        },
                    }
                }
            }
        )
    )
    defs = root / "modules/arc-runners/defs"
    defs.mkdir(parents=True)
    (defs / "probe.yaml").write_text(yaml.safe_dump({"runner": {"name": "l-x86iamx-8-16", "max_runners": 7}}))
    monkeypatch.setenv("CLUSTERS_YAML", str(module.UPSTREAM / "clusters.yaml"))
    target = module.DrainTest(controller.args, "root123")
    recipe = Mock()
    monkeypatch.setattr(target, "recipe", recipe)
    command = module.command

    def run_map(argv, **kwargs):
        assert argv[0:2] == ["uv", "run"]
        # Execute the real helper offline with this test's interpreter.
        return command([sys.executable, *argv[2:]], **kwargs)

    monkeypatch.setattr(module, "command", run_map)
    target.prepare()
    recipe.assert_called_once_with("kubeconfig", timeout=90)
    assert target.group == "root-runners"
    assert target.probe_label == "root-l-x86iamx-8-16"
    assert target.desired == {target.probe_label: 7}
    assert target.env_overrides["CLUSTERS_YAML"] == str(root / "clusters.yaml")


@pytest.mark.parametrize(
    ("cluster_prefix", "default_prefix", "expected_prefix"),
    [("renamed-", "default-", "renamed-"), (None, "default-", ""), (None, None, ""), ("", "default-", "")],
)
def test_configured_prefix_drives_probe_and_workflow(
    controller, monkeypatch, cluster_prefix, default_prefix, expected_prefix
):
    arc_config = {"runner_group": CLUSTER, "github_config_url": ORG_URL}
    if cluster_prefix is not None:
        arc_config["runner_name_prefix"] = cluster_prefix
    defaults = {"arc-runners": {"runner_name_prefix": default_prefix}} if default_prefix is not None else {}
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump({"clusters": {CLUSTER: {"arc-runners": arc_config}}, "defaults": defaults})
    )
    monkeypatch.setattr(module, "command", Mock(return_value=subprocess.CompletedProcess([], 0, '{"cpu":8}', "")))
    controller.prepare()
    expected_label = f"{expected_prefix}l-x86iamx-8-16"
    assert controller.probe_label == expected_label
    items = ars()
    items[0]["spec"]["runnerScaleSetName"] = expected_label
    assert controller.check_probe(items) == "cpu"
    with pytest.raises(ValueError, match="No enabled scale set"):
        controller.check_probe(ars())
    workflow = yaml.safe_load(controller.workflow())
    for job in workflow["jobs"].values():
        assert job["runs-on"] == {"group": CLUSTER, "labels": [expected_label]}
    assert "{{PROBE_LABEL}}" not in controller.workflow()


@pytest.mark.parametrize("url", [ORG_URL, f"{ORG_URL}/"])
def test_organization_url_preserves_probe_group_and_labels(controller, monkeypatch, url):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump({"clusters": {CLUSTER: {"arc-runners": {"github_config_url": url, "runner_group": CLUSTER}}}})
    )
    monkeypatch.setattr(module, "command", Mock(return_value=subprocess.CompletedProcess([], 0, '{"cpu":8}', "")))
    controller.prepare()
    for probe in yaml.safe_load(controller.workflow())["jobs"].values():
        assert probe["runs-on"] == {"group": CLUSTER, "labels": ["l-x86iamx-8-16"]}


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        123,
        "https://github.com",
        "https://github.com/",
        f"{ORG_URL}//",
        f"{ORG_URL}?query=value",
        f"{ORG_URL}/?query=value",
        f"{ORG_URL}#fragment",
        f"{ORG_URL}/#fragment",
        f"{ORG_URL} extra",
        "https://github.com/enterprises/pytorch",
    ],
)
def test_unsupported_registration_preflight_is_read_only(controller, monkeypatch, url):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"arc-runners": {"github_config_url": ORG_URL, "runner_group": "other"}},
                "clusters": {CLUSTER: {"arc-runners": {"github_config_url": url, "runner_group": CLUSTER}}},
            }
        )
    )
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match=r"valid arc-runners.github_config_url"):
        controller.prepare()
    command.assert_not_called()


@pytest.mark.parametrize("url", [f"{ORG_URL}/pytorch-canary", f"{ORG_URL}/pytorch-canary/"])
@pytest.mark.parametrize("group", [CLUSTER, "default"])
def test_repository_registration_preflight_is_read_only(controller, monkeypatch, url, group):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"arc-runners": {"github_config_url": ORG_URL, "runner_group": "other"}},
                "clusters": {CLUSTER: {"arc-runners": {"github_config_url": url, "runner_group": group}}},
            }
        )
    )
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match="organization-scoped"):
        controller.prepare()
    command.assert_not_called()


def test_registration_url_does_not_fall_back_per_key(controller, monkeypatch):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"arc-runners": {"github_config_url": ORG_URL, "runner_group": "other"}},
                "clusters": {CLUSTER: {"arc-runners": {"runner_group": CLUSTER}}},
            }
        )
    )
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match=r"valid arc-runners.github_config_url"):
        controller.prepare()
    command.assert_not_called()


@pytest.mark.parametrize("group", [CLUSTER, "default"])
def test_repository_registration_from_defaults_is_read_only(controller, monkeypatch, group):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"arc-runners": {"github_config_url": f"{ORG_URL}/pytorch-canary", "runner_group": group}},
                "clusters": {CLUSTER: {}},
            }
        )
    )
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match="organization-scoped"):
        controller.prepare()
    command.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "url", "cluster", "group"),
    [
        ([], f"{ORG_URL}/pytorch-canary", CLUSTER, CLUSTER),
        (["--dry-run"], f"{ORG_URL}/pytorch-canary/", "meta-staging-aws-ue1", "default"),
        (["--restore-only"], f"{ORG_URL}/pytorch-canary", CLUSTER, "default"),
        (["--defer-recovery"], f"{ORG_URL}/pytorch-canary/", "meta-staging-aws-ue1", CLUSTER),
    ],
    ids=["live", "dry-run", "restore-only", "defer-recovery"],
)
def test_cli_repository_registration_is_read_only(
    controller, monkeypatch, ci_environment, capsys, mode, url, cluster, group
):
    state = controller.args.root_dir / "intent.json"
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump({"clusters": {cluster: {"arc-runners": {"github_config_url": url, "runner_group": group}}}})
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "test",
            "--cluster-id",
            cluster,
            "--root-dir",
            str(controller.args.root_dir),
            "--recovery-file",
            str(state),
            *mode,
        ],
    )
    operations = [Mock() for _ in range(6)]
    command, constructor, run, signals, register, kill = operations
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "DrainTest", constructor)
    monkeypatch.setattr(module, "run_controller", run)
    monkeypatch.setattr(module.signal, "signal", signals)
    monkeypatch.setattr(module.atexit, "register", register)
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(ValueError, match="organization-scoped"):
        module.main()
    for operation in operations:
        operation.assert_not_called()
    assert not capsys.readouterr().out
    assert not state.exists()


@pytest.mark.parametrize(
    "arc_config",
    [
        pytest.param({"runner_group": ""}, id="empty-group"),
        pytest.param({"runner_group": 123}, id="invalid-group-type"),
        pytest.param({}, id="empty-block-does-not-inherit-group"),
        pytest.param({"github_config_url": "https://github.com/pytorch"}, id="partial-block-does-not-inherit-group"),
    ],
)
def test_missing_group_preflight_is_read_only(controller, monkeypatch, arc_config):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "defaults": {"arc-runners": {"runner_group": CLUSTER}},
                "clusters": {CLUSTER: {"arc-runners": arc_config}},
            }
        )
    )
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match="Expected runner group"):
        controller.prepare()
    command.assert_not_called()


def test_invalid_arc_config_preflight_is_read_only(controller, monkeypatch):
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump({"clusters": {CLUSTER: {"arc-runners": []}}})
    )
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(ValueError, match="Expected arc-runners configuration to be a mapping"):
        controller.prepare()
    command.assert_not_called()


def test_unknown_cluster_preflight_is_read_only(controller, monkeypatch, caplog):
    (controller.args.root_dir / "clusters.yaml").write_text(yaml.safe_dump({"clusters": {}}))
    command = Mock()
    monkeypatch.setattr(module, "command", command)
    with pytest.raises(SystemExit) as exc:
        controller.prepare()
    assert exc.value.code == 1
    assert f"Cluster '{CLUSTER}' not found" in caplog.text
    command.assert_not_called()


@pytest.mark.parametrize("bounded", [False, True])
def test_scale_sets_and_live_metrics(controller, monkeypatch, bounded):
    # listener_pods() identifies the listener by Pod label; the CR is no longer consulted.
    pod = {
        "metadata": {"name": "cpu-7f4d9b7c4f-listener", "labels": {SCALE_SET_LABEL: "cpu"}},
        "spec": {"containers": [{"ports": [{"name": "metrics", "containerPort": 8080}]}]},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    responses = iter(
        [
            json.dumps({"items": ars()}),
            json.dumps({"items": [pod]}),
            f'{CAPACITY_METRIC}{{name="cpu-ers"}} 0\n',
        ]
    )

    def read(*args, **kwargs):
        if "--raw" in args:
            assert kwargs.get("deadline") == deadline
        return next(responses)

    deadline = time.monotonic() + 5 if bounded else None
    kube = Mock(side_effect=read)
    monkeypatch.setattr(module, "kubectl", kube)
    assert controller.scale_sets() == ars()
    with controller.deadline_scope(deadline) if bounded else contextlib.nullcontext():
        assert controller.capacities() == {"cpu": 0}
    assert kube.call_args.args == (
        "get",
        "--raw",
        "/api/v1/namespaces/arc-systems/pods/cpu-7f4d9b7c4f-listener:8080/proxy/metrics",
    )


@pytest.mark.parametrize("expected", [None, {"cpu"}, set()])
def test_capacities_scrapes_only_selected_listeners(controller, monkeypatch, expected):
    controller.desired = {"cpu": 8, **{f"other-{i}": 0 for i in range(48)}}
    pods = [
        {
            "metadata": {"name": f"{name}-7f4d9b7c4f-listener", "labels": {SCALE_SET_LABEL: name}},
            "spec": {"containers": [{"ports": [{"name": "metrics", "containerPort": 8080}]}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for name in controller.desired
    ]
    metrics = {
        (
            f"/api/v1/namespaces/arc-systems/pods/{name}-7f4d9b7c4f-listener:8080/proxy/metrics"
        ): f'{CAPACITY_METRIC}{{name="{name}-ers"}} 0\n'
        for name in controller.desired
    }

    def read(*args, **kwargs):
        if args[1] == "pods":
            return json.dumps({"items": pods})
        return metrics[args[2]]

    kube = Mock(side_effect=read)
    monkeypatch.setattr(controller, "kube", kube)
    if expected == set():
        with pytest.raises(ValueError, match="Missing ready listeners"):
            controller.capacities(expected)
        assert kube.call_count == 1
    else:
        names = set(controller.desired) if expected is None else expected
        assert controller.capacities(expected) == dict.fromkeys(names, 0)
        assert kube.call_count == 1 + len(names)
        assert {call.args[2] for call in kube.call_args_list if "--raw" in call.args} == {
            f"/api/v1/namespaces/arc-systems/pods/{name}-7f4d9b7c4f-listener:8080/proxy/metrics" for name in names
        }


@pytest.mark.parametrize(
    "interruption", [ValueError("missing metric"), TimeoutError("batch deadline"), SystemExit(130)]
)
def test_abandoned_metrics_batch_stops_running_commands_and_preserves_error(controller, monkeypatch, interruption):
    controller.deadline = time.monotonic() + 10
    controller.desired = {f"cpu-{i}": 8 for i in range(20)}
    listeners = {name: (name, 8080) for name in controller.desired}
    monkeypatch.setattr(module, "listener_pods", Mock(return_value=listeners))
    launch = subprocess.Popen
    unrelated = launch([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    processes, futures = [], []
    started, lock = Event(), Lock()

    def start(*args, **kwargs):
        proc = launch(*args, **kwargs)
        with lock:
            processes.append(proc)
            if len(processes) == 8:
                started.set()
        return proc

    def kube(*args, **kwargs):
        if "--raw" not in args:
            return '{"items": []}'
        return module.command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            deadline=kwargs["deadline"],
            cancel_event=kwargs["cancel_event"],
        ).stdout

    class InterruptedPool(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            futures.append(future)
            return future

        def map(self, *args, **kwargs):
            self.results = super().map(*args, **kwargs)
            assert started.wait(3), "Metric subprocesses did not start"
            raise interruption

    monkeypatch.setattr(module.subprocess, "Popen", start)
    monkeypatch.setattr(module, "kubectl", kube)
    monkeypatch.setattr(module, "ThreadPoolExecutor", InterruptedPool)
    try:
        with pytest.raises(type(interruption)) as exc:
            controller.capacities()
        assert exc.value is interruption
        controller.wait_for("cancelled metrics finished", lambda: all(f.done() for f in futures), 3, poll=0.01)
        assert len(processes) == 8  # Queued tasks cannot launch more commands.
        assert all(proc.poll() is not None for proc in processes)
        assert all(f.cancelled() or isinstance(f.exception(), CancelledError) for f in futures)
        assert unrelated.poll() is None

        # A later observation has an independent cancellation flag.
        def recovered_kube(*args, **kwargs):
            if "--raw" not in args:
                return '{"items": []}'
            assert not kwargs["cancel_event"].is_set()
            name = args[2].split("/pods/", 1)[1].split(":", 1)[0]
            return f'{CAPACITY_METRIC}{{name="{name}"}} 0\n'

        monkeypatch.setattr(module, "ThreadPoolExecutor", ThreadPoolExecutor)
        monkeypatch.setattr(module, "kubectl", recovered_kube)
        assert controller.capacities() == dict.fromkeys(controller.desired, 0)
    finally:
        for proc in [*processes, unrelated]:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)


def test_canary_pr_tree_and_branch(controller, monkeypatch):
    next_run = module.DrainTest(controller.args, "def456")
    assert controller.branch == next_run.branch == f"osdc-drain-test-{CLUSTER}"
    next_run.probe_label = controller.probe_label
    assert controller.workflow_name != next_run.workflow_name
    assert "OSDC_DRAIN_RELEASE_abc123" in controller.workflow()
    assert "OSDC_DRAIN_RELEASE_def456" in next_run.workflow()
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
    assert controller.canary_cleanup_needed
    assert api.call_args_list[3].args[1] == {
        "base_tree": "base-tree",
        "tree": [{"path": ".github/workflows", "mode": "040000", "type": "tree", "sha": "workflows"}],
    }
    ref = api.call_args_list[5].args[1]
    assert ref == {"ref": f"refs/heads/{controller.branch}", "sha": "commit"}
    assert ref["ref"] != "refs/heads/main"
    assert api.call_args_list[6].args[1]["title"] == f"{module.PR_TITLE_PREFIX} abc123"
    assert "OSDC_DRAIN_RELEASE_abc123" not in api.call_args_list[6].args[1]["body"]


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("gh", 30), subprocess.CalledProcessError(1, "gh")])
def test_ref_creation_failure_retains_cleanup_intent(controller, monkeypatch, error):
    responses = iter(
        [
            {"object": {"sha": "base"}},
            {"tree": {"sha": "base-tree"}},
            {"sha": "workflows"},
            {"sha": "tree"},
            {"sha": "commit"},
        ]
    )

    def request(endpoint, *args, **kwargs):
        assert controller.canary_cleanup_needed is (endpoint == "git/refs")
        if endpoint == "git/refs":
            raise error
        return next(responses)

    api = Mock(side_effect=request)
    monkeypatch.setattr(module, "api", api)
    with pytest.raises(type(error)):
        controller.create_pr()
    assert controller.canary_cleanup_needed
    monkeypatch.setattr(controller, "runs", list)
    api.side_effect = [
        [],  # No open PRs on the failed branch.
        subprocess.CalledProcessError(1, "gh", stderr="Reference does not exist (HTTP 422)"),
        [],  # Matching-refs lookup confirms the branch is gone.
    ]
    assert controller.cleanup()
    assert not controller.canary_cleanup_needed


@pytest.mark.parametrize("status", ["completed", "queued", "in_progress"])
@pytest.mark.parametrize("cancel_error", [subprocess.TimeoutExpired("gh", 30), subprocess.CalledProcessError(1, "gh")])
def test_cancel_run_rechecks_status_after_failure(controller, monkeypatch, status, cancel_error):
    api = Mock(side_effect=[cancel_error, {"status": status}])
    monkeypatch.setattr(controller, "api", api)
    if status == "completed":
        controller.cancel_run(canary_run(1, "queued"))
    else:
        with pytest.raises(type(cancel_error)) as exc:
            controller.cancel_run(canary_run(1, "queued"))
        assert exc.value is cancel_error
    assert api.call_args_list == [
        call("actions/runs/1/cancel", method="POST"),
        call("actions/runs/1"),
    ]


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("status_error", [subprocess.TimeoutExpired("gh", 30), subprocess.CalledProcessError(1, "gh")])
def test_cancel_run_preserves_original_error_when_status_recheck_fails(
    controller, monkeypatch, caplog, startup, status_error
):
    run = canary_run(1, "queued")
    cancel_error = subprocess.TimeoutExpired("gh api actions/runs/1/cancel", 30)
    responses = [cancel_error, status_error]
    calls = [call("actions/runs/1/cancel", method="POST"), call("actions/runs/1")]
    if startup:
        responses = [{"workflow_runs": [run]}, [], *responses]
        calls = [
            call(f"actions/runs?event=pull_request&branch={controller.branch}&per_page=100&page=1"),
            call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
            *calls,
        ]
    api = Mock(side_effect=responses)
    monkeypatch.setattr(controller, "api", api)
    operation = module.DrainTest.cleanup_stale_canary if startup else module.DrainTest.cancel_run
    args = (controller,) if startup else (controller, run)
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        operation(*args)
    assert exc.value is cancel_error
    assert api.call_args_list == calls
    record = next(record for record in caplog.records if "Status recheck failed" in record.getMessage())
    assert record.getMessage() == "Status recheck failed after cancellation failed for run 1"
    assert record.exc_info[1] is status_error


@pytest.mark.parametrize("status", [404, 422])
@pytest.mark.parametrize("similar_ref", [False, True])
def test_delete_canary_branch_accepts_confirmed_absence(controller, monkeypatch, status, similar_ref):
    refs = [{"ref": f"refs/heads/{controller.branch}-other"}] if similar_ref else []
    api = Mock(
        side_effect=[subprocess.CalledProcessError(1, "gh", stderr=f"Reference does not exist (HTTP {status})"), refs]
    )
    monkeypatch.setattr(controller, "api", api)
    controller.delete_canary_branch()
    assert api.call_args_list == [
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
        call(f"git/matching-refs/heads/{controller.branch}"),
    ]


@pytest.mark.parametrize("failure", ["delete", "lookup"])
def test_delete_canary_branch_propagates_unconfirmed_failure(controller, monkeypatch, failure):
    delete_error = subprocess.CalledProcessError(1, "gh", stderr="Deletion failed (HTTP 422)")
    lookup_error = subprocess.CalledProcessError(1, "gh", stderr="Lookup failed (HTTP 403)")
    api = Mock(
        side_effect=[
            delete_error,
            lookup_error if failure == "lookup" else [{"ref": f"refs/heads/{controller.branch}"}],
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        controller.delete_canary_branch()
    assert exc.value is (lookup_error if failure == "lookup" else delete_error)


def test_delete_canary_branch_timeout_confirms_absence(controller, monkeypatch):
    api = Mock(
        side_effect=[
            subprocess.TimeoutExpired("gh", 30),
            [],  # Matching-refs lookup confirms the branch is already gone.
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    controller.delete_canary_branch()
    assert api.call_args_list == [
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
        call(f"git/matching-refs/heads/{controller.branch}"),
    ]


def test_delete_canary_branch_timeout_propagates_when_still_present(controller, monkeypatch):
    api = Mock(
        side_effect=[
            subprocess.TimeoutExpired("gh", 30),
            [{"ref": f"refs/heads/{controller.branch}"}],
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(subprocess.TimeoutExpired):
        controller.delete_canary_branch()
    assert api.call_args_list == [
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
        call(f"git/matching-refs/heads/{controller.branch}"),
    ]


def test_startup_removes_orphan_branch_without_pr_before_creating_canary(controller, monkeypatch):
    test = module.DrainTest(controller.args, "next123")
    test.desired = controller.desired
    test.probe_label = controller.probe_label
    branch_exists = True
    events = []

    def api(endpoint, payload=None, method="GET"):
        nonlocal branch_exists
        if endpoint.startswith("actions/runs?"):
            return {"workflow_runs": []}
        if endpoint.startswith("pulls?"):
            return []  # The previous process died before creating its PR.
        if endpoint.startswith("git/matching-refs/"):
            return [{"ref": f"refs/heads/{test.branch}"}] if branch_exists else []
        if endpoint == f"git/refs/heads/{test.branch}" and method == "DELETE":
            branch_exists = False
            events.append("delete-orphan")
            return {}
        if endpoint == "git/ref/heads/main":
            return {"object": {"sha": "base"}}
        if endpoint == "git/commits/base":
            return {"tree": {"sha": "base-tree"}}
        if endpoint in {"git/trees", "git/commits"}:
            return {"sha": "new-object"}
        if endpoint == "git/refs":
            if branch_exists:
                raise subprocess.CalledProcessError(1, "gh api", stderr="Reference already exists (HTTP 422)")
            branch_exists = True
            events.append("create-branch")
            return {}
        assert endpoint == "pulls"
        assert method == "POST"
        events.append("create-pr")
        return {"number": 4}

    monkeypatch.setattr(test, "api", api)
    monkeypatch.setattr(test, "prepare", Mock())
    monkeypatch.setattr(test, "scale_sets", lambda: ars())
    monkeypatch.setattr(test, "capacities", lambda: {"cpu": 0})
    monkeypatch.setattr(test, "find_run", Mock(side_effect=RuntimeError("new canary created")))
    with pytest.raises(RuntimeError, match="new canary created"):
        test.run()
    assert events == ["delete-orphan", "create-branch", "create-pr"]
    assert test.pr == 4


@pytest.mark.parametrize("branch_exists", [False, True])
@pytest.mark.parametrize("pr_exists", [False, True])
def test_startup_cleanup_cancels_runs_then_closes_pr_then_deletes_only_exact_ref(
    controller, monkeypatch, branch_exists, pr_exists
):
    ref = f"refs/heads/{controller.branch}"
    refs = [{"ref": f"{ref}-other"}, *([{"ref": ref}] if branch_exists else [])]
    responses = [
        {
            "workflow_runs": [
                canary_run(1, "queued"),
                canary_run(2, "in_progress"),
                canary_run(3, "completed"),
                {"id": 9, "status": "completed", "name": "Foreign PR workflow"},
            ]
        },
        [canary_pr(4)] if pr_exists else [],
        {},
        {},
        *([{}] if pr_exists else []),
        {} if branch_exists else subprocess.CalledProcessError(1, "gh"),
        *([] if branch_exists else [refs]),
    ]
    api = Mock(side_effect=responses)
    monkeypatch.setattr(controller, "api", api)
    module.DrainTest.cleanup_stale_canary(controller)
    assert api.call_args_list == [
        call(f"actions/runs?event=pull_request&branch={controller.branch}&per_page=100&page=1"),
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
        call("actions/runs/1/cancel", method="POST"),
        call("actions/runs/2/cancel", method="POST"),
        *([call("pulls/4", {"state": "closed"}, "PATCH")] if pr_exists else []),
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
        *([] if branch_exists else [call(f"git/matching-refs/heads/{controller.branch}")]),
    ]
    assert not controller.canary_cleanup_needed  # Startup reclamation does not register a new canary's intent.


def test_startup_cleanup_finds_active_runs_beyond_first_page(controller, monkeypatch):
    api = Mock(
        side_effect=[
            {"workflow_runs": [canary_run(i, "completed") for i in range(100)]},
            {"workflow_runs": [canary_run(101, "queued")]},
            [],
            {},
            {},
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    module.DrainTest.cleanup_stale_canary(controller)
    assert api.call_args_list[:4] == [
        call(f"actions/runs?event=pull_request&branch={controller.branch}&per_page=100&page=1"),
        call(f"actions/runs?event=pull_request&branch={controller.branch}&per_page=100&page=2"),
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
        call("actions/runs/101/cancel", method="POST"),
    ]


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
def test_startup_cleanup_ignores_foreign_runs(controller, monkeypatch, caplog, status):
    known_run = canary_run(1, "queued")
    foreign_run = {**canary_run(2, status), "name": "Foreign PR workflow", "id": 2}
    responses = [
        {"workflow_runs": [known_run, foreign_run]},
        [],
        {},
        {},
    ]
    api = Mock(side_effect=responses)
    monkeypatch.setattr(controller, "api", api)
    module.DrainTest.cleanup_stale_canary(controller)
    assert api.call_args_list == [
        call(f"actions/runs?event=pull_request&branch={controller.branch}&per_page=100&page=1"),
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
        call("actions/runs/1/cancel", method="POST"),
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
    ]
    assert "Ignoring 1 foreign workflow run(s)" in caplog.text
    assert not controller.canary_cleanup_needed


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed"])
def test_startup_cleanup_ignores_foreign_runs_beyond_first_page(controller, monkeypatch, status):
    foreign_run = {**canary_run(2, status), "name": "Foreign PR workflow", "id": 2}
    api = Mock(
        side_effect=[
            {"workflow_runs": [canary_run(i, "completed") for i in range(100)]},
            {"workflow_runs": [foreign_run]},
            [],
            {},
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    module.DrainTest.cleanup_stale_canary(controller)
    assert call("actions/runs/2/cancel", method="POST") not in api.call_args_list
    assert call(f"git/refs/heads/{controller.branch}", method="DELETE") in api.call_args_list
    assert not controller.canary_cleanup_needed


@pytest.mark.parametrize(
    ("identity", "status"),
    [
        (None, "queued"),
        ("", "in_progress"),
        ("unrelated", "completed"),
        ("OSDC Drain Test ", "queued"),
        ("OSDC Drain Test old/run", "in_progress"),
        ("OSDC Drain Test ٣", "completed"),
        ("OSDC Drain Test abc中文", "queued"),
    ],
)
def test_reclaim_rejects_foreign_run_before_any_cleanup_writes(controller, monkeypatch, identity, status):
    foreign_run = {**canary_run(2, status), "name": identity}
    cancel, api, delete_branch = Mock(), Mock(), Mock()
    monkeypatch.setattr(controller, "cancel_run", cancel)
    monkeypatch.setattr(controller, "api", api)
    monkeypatch.setattr(controller, "delete_canary_branch", delete_branch)
    with pytest.raises(RuntimeError, match="Refusing canary cleanup: unrecognized workflow run"):
        controller._reclaim([canary_run(1, "queued"), foreign_run], [canary_pr(4)])
    cancel.assert_not_called()
    api.assert_not_called()
    delete_branch.assert_not_called()


@pytest.mark.parametrize(
    "identity",
    [None, "", "unrelated", "OSDC Drain Test ", "OSDC Drain Test old/run", f"{module.PR_TITLE_PREFIX} ٣"],
)
def test_startup_cleanup_refuses_unknown_pr(controller, monkeypatch, identity):
    responses = [
        {"workflow_runs": [canary_run(1, "queued")]},
        [{"number": 4, "title": identity}],
    ]
    api = Mock(side_effect=responses)
    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(RuntimeError, match="Refusing canary cleanup: unrecognized PR"):
        module.DrainTest.cleanup_stale_canary(controller)
    assert api.call_args_list == [
        call(f"actions/runs?event=pull_request&branch={controller.branch}&per_page=100&page=1"),
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
    ]
    assert not controller.canary_cleanup_needed


def test_exit_cleanup_preserves_unknown_pr_after_cluster_recovery(controller, monkeypatch):
    controller.draining = controller.canary_cleanup_needed = True

    def resume():
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "runs", lambda: [canary_run(1, "queued")])
    api = Mock(side_effect=[[{"number": 4, "title": "unrelated"}]])
    monkeypatch.setattr(controller, "api", api)
    assert not controller.cleanup()
    assert not controller.draining
    assert controller.canary_cleanup_needed
    api.assert_called_once_with(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100")


@pytest.mark.parametrize("failure", ["list-runs", "cancel-run", "list-prs", "close-pr", "list-refs", "delete-ref"])
def test_startup_cleanup_failure_stops_before_preflight_or_canary_creation(controller, monkeypatch, failure):
    monkeypatch.delattr(controller, "cleanup_stale_canary")
    prepare, sets, create = Mock(), Mock(), Mock()
    monkeypatch.setattr(controller, "prepare", prepare)
    monkeypatch.setattr(controller, "scale_sets", sets)
    monkeypatch.setattr(controller, "create_pr", create)

    def api(endpoint, payload=None, method="GET"):
        if endpoint.startswith("actions/runs?"):
            operation, result = "list-runs", {"workflow_runs": [canary_run(1, "queued")]}
        elif endpoint == "actions/runs/1/cancel":
            operation, result = "cancel-run", {}
        elif endpoint == "actions/runs/1":
            return {"status": "in_progress"}  # Cancellation really failed.
        elif endpoint.startswith("pulls?"):
            operation, result = "list-prs", [canary_pr(4)]
        elif endpoint == "pulls/4":
            operation, result = "close-pr", {}
        elif endpoint.startswith("git/matching-refs/"):
            operation, result = "list-refs", [{"ref": f"refs/heads/{controller.branch}"}]
        else:
            assert endpoint == f"git/refs/heads/{controller.branch}"
            assert method == "DELETE"
            operation, result = "delete-ref", {}
            if failure == "list-refs":
                raise subprocess.CalledProcessError(1, "gh api", stderr=operation)
        if operation == failure:
            raise subprocess.CalledProcessError(1, "gh api", stderr=operation)
        return result

    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        controller.run()
    assert exc.value.stderr == failure
    prepare.assert_called_once()
    sets.assert_not_called()
    create.assert_not_called()
    assert not controller.draining
    assert not controller.canary_cleanup_needed


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
    running["runner_group_name"] = None
    assert controller.inflight_ready()  # tolerate transient null while runner_id is set
    running["runner_group_name"] = "other"
    with pytest.raises(RuntimeError, match="different runner group"):
        controller.inflight_ready()
    jobs.return_value = [job()]
    with pytest.raises(RuntimeError, match="finished before drain"):
        controller.inflight_ready()


@pytest.mark.parametrize("strict", [False, True])
def test_drained_requires_config_and_capacity(controller, monkeypatch, strict):
    sets = Mock(return_value=ars(0))
    caps = Mock(return_value={"cpu": 0})
    monkeypatch.setattr(controller, "scale_sets", sets)
    monkeypatch.setattr(controller, "capacities", caps)
    assert controller.drained(strict=strict)
    error = RuntimeError if strict else ValueError
    caps.return_value = {"cpu": 2147483647}
    with pytest.raises(error, match="still advertise capacity"):
        controller.drained(strict=strict)
    sets.return_value = ars(8)
    with pytest.raises(error, match="Runner limits differ"):
        controller.drained(strict=strict)


@pytest.mark.parametrize("source", ["limits", "capacity"])
def test_initial_drain_waits_for_convergence(controller, monkeypatch, clock, source):
    sets = Mock(side_effect=[ars(8), ars(0)]) if source == "limits" else Mock(return_value=ars(0))
    caps = Mock(side_effect=[{"cpu": 8}, {"cpu": 0}]) if source == "capacity" else Mock(return_value={"cpu": 0})
    monkeypatch.setattr(controller, "scale_sets", sets)
    monkeypatch.setattr(controller, "capacities", caps)
    controller.wait_for("initial drain", controller.drained, timeout=30, poll=5)
    assert clock[0] == 5


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
    jobs.return_value = [job("held", status="queued", job_id=i) for i in range(1, 4)]
    assert controller.held_ready()
    jobs.return_value = [job("held", job_id=i) for i in range(1, 4)]
    with pytest.raises(RuntimeError, match="escaped"):
        controller.held_ready()


@pytest.fixture
def observing_controller(controller, monkeypatch):
    monkeypatch.setattr(
        controller, "jobs", Mock(return_value=[job("held", status="queued", job_id=i) for i in range(1, 4)])
    )
    monkeypatch.setattr(controller, "scale_sets", Mock(return_value=ars(0)))
    monkeypatch.setattr(controller, "capacities", Mock(return_value={"cpu": 0}))
    return controller


def test_observation_returns_after_first_full_check(observing_controller, clock):
    test = observing_controller
    test.desired["other"] = 8
    test.scale_sets.return_value = [*ars(0), {"metadata": {"name": "other"}, "spec": {"maxRunners": 0}}]
    test.capacities.return_value = {"cpu": 0, "other": 0}
    test.observe_held()
    assert clock[0] == 0
    test.jobs.assert_called_once()
    test.scale_sets.assert_called_once()
    test.capacities.assert_called_once_with()


@pytest.mark.parametrize("positive_capacity", [False, True])
def test_observation_retries_sweep_all_listeners(observing_controller, clock, positive_capacity):
    test = observing_controller
    test.desired["other"] = 8
    test.scale_sets.return_value = [*ars(0), {"metadata": {"name": "other"}, "spec": {"maxRunners": 0}}]
    test.capacities.side_effect = [
        ValueError("Missing ready listeners: ['other']"),
        {"cpu": 0, "other": int(positive_capacity)},
    ]
    if positive_capacity:
        with pytest.raises(RuntimeError, match="still advertise capacity"):
            test.observe_held()
    else:
        test.observe_held()
    assert clock[0] == 10
    assert test.capacities.call_args_list == [call(), call()]
    assert test.jobs.call_count == test.scale_sets.call_count == 2


@pytest.mark.parametrize(
    ("source", "failure"),
    [
        ("jobs", subprocess.CalledProcessError(1, ["gh", "api"])),
        ("scale_sets", subprocess.CalledProcessError(1, ["kubectl", "get"])),
        ("capacities", subprocess.TimeoutExpired("kubectl", 30)),
        ("capacities", ValueError("Missing ready listeners")),
        ("capacities", ValueError("Missing or invalid gha_capacity_advertised_max_runners")),
        ("scale_sets", ValueError("Invalid JSON snapshot")),
    ],
)
def test_observation_retries_transient_reads(observing_controller, clock, source, failure):
    read = getattr(observing_controller, source)
    read.side_effect = [failure, read.return_value]
    observing_controller.observe_held()
    assert read.call_count == 2
    assert clock[0] == 10
    assert all(item == call() for item in observing_controller.capacities.call_args_list)


@pytest.mark.parametrize("snapshot", [[], ars(-1)])
def test_observation_retries_unavailable_runner_snapshot(observing_controller, clock, snapshot):
    sets = observing_controller.scale_sets
    sets.side_effect = [snapshot, ars(0)]
    observing_controller.observe_held()
    assert clock[0] == 10
    assert sets.call_count == 2


@pytest.mark.parametrize("failure", ["assigned", "completed", "limits", "capacity"])
@pytest.mark.parametrize("after_read_failure", [False, True])
def test_observation_fails_immediately_on_regression(observing_controller, clock, failure, after_read_failure):
    test = observing_controller
    if failure in ("assigned", "completed"):
        changed = "runner_id" if failure == "assigned" else "status"
        value = 123 if failure == "assigned" else "completed"
        queued = test.jobs.return_value
        escaped = [{**item, changed: value} for item in queued]
        test.jobs.side_effect = [queued, escaped] if after_read_failure else [escaped]
        message = "Jobs escaped"
    elif failure == "limits":
        test.scale_sets.side_effect = [ars(0), ars(8)] if after_read_failure else [ars(8)]
        message = "Runner limits differ"
    else:
        test.capacities.return_value = {"cpu": 1}
        message = "still advertise capacity"
    if after_read_failure:
        test.capacities.side_effect = [ValueError("Missing ready listeners"), test.capacities.return_value]
    with pytest.raises(RuntimeError, match=message):
        test.observe_held()
    assert clock[0] == (10 if after_read_failure else 0)
    assert test.jobs.call_count == (2 if after_read_failure else 1)


@pytest.mark.parametrize("recover_at", [35, 95, 110, 120, 125, None])
@pytest.mark.parametrize(
    "failure", ["Missing ready listeners", "Missing or invalid gha_capacity_advertised_max_runners"]
)
def test_observation_retries_share_total_budget(observing_controller, clock, recover_at, failure):
    test = observing_controller
    deadlines = []

    def capacities(expected=None):
        assert expected is None
        deadlines.append(test.deadline)
        if recover_at is None or clock[0] < recover_at:
            raise ValueError(failure)
        return {"cpu": 0}

    test.capacities.side_effect = capacities
    if recover_at is not None and recover_at < 120:
        test.observe_held()
        assert clock[0] == ((recover_at + 9) // 10) * 10
    else:
        with pytest.raises(TimeoutError, match=rf"drain still in effect: deadline exceeded.*{failure}"):
            test.observe_held()
        assert clock[0] == 120
    assert deadlines == [120] * len(deadlines)
    assert test.jobs.call_count == test.scale_sets.call_count == len(deadlines)
    assert test.deadline is None


@pytest.mark.parametrize("check_seconds", [35, 119, 120, 121])
def test_observation_slow_check_consumes_total_budget(observing_controller, clock, check_seconds):
    test = observing_controller

    def capacities(expected=None):
        assert expected is None
        assert test.remaining(float("inf")) == 120
        clock[0] += check_seconds
        return {"cpu": 0}

    test.capacities.side_effect = capacities
    if check_seconds < 120:
        test.observe_held()
    else:
        with pytest.raises(TimeoutError, match="drain still in effect: deadline exceeded"):
            test.observe_held()
    assert clock[0] == check_seconds
    test.capacities.assert_called_once_with()


@pytest.mark.parametrize("parent_deadline", [85, 195])
def test_observation_respects_parent_deadline(observing_controller, clock, parent_deadline):
    test = observing_controller
    test.deadline = parent_deadline
    test.capacities.side_effect = ValueError("Missing ready listeners")
    with pytest.raises(TimeoutError, match=r"drain still in effect: deadline exceeded.*Missing ready listeners"):
        test.observe_held()
    assert clock[0] == min(parent_deadline, 120)
    assert test.deadline == parent_deadline


def test_resume_verifies_limits_and_taints(controller, monkeypatch):
    controller.draining = True
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    sets = Mock(return_value=ars(8))
    monkeypatch.setattr(controller, "scale_sets", sets)
    kube = Mock(return_value='{"items":[]}')
    monkeypatch.setattr(module, "kubectl", kube)
    controller.resume()
    recipe.assert_called_once_with("resume-runners", timeout=210)
    assert not controller.draining
    controller.draining = True
    controller.resume_limits_restored = False
    sets.return_value = ars(0)
    with pytest.raises(ValueError, match="Runner limits differ"):
        controller.resume()
    assert controller.draining
    sets.return_value = ars(8)


def refresh_node(uid="old", *, tainted=True):
    return {
        "metadata": {"name": "runner", "uid": uid},
        "spec": {
            "taints": [
                {
                    "key": module.REFRESH_TAINT_KEY,
                    "value": module.REFRESH_TAINT_VALUE,
                    "effect": module.REFRESH_TAINT_EFFECT,
                }
            ]
            if tainted
            else []
        },
    }


@pytest.mark.parametrize("state", ["empty", "deleted", "replaced", "survives", "failed"])
def test_taint_runner_nodes_handles_recycling(controller, monkeypatch, state):
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    before = [] if state == "empty" else [refresh_node(tainted=False)]
    after = {
        "empty": [],
        "deleted": [],
        "replaced": [refresh_node("new", tainted=False)],
        "survives": [refresh_node()],
        "failed": [refresh_node(tainted=False)],
    }[state]
    monkeypatch.setattr(controller, "runner_nodes", Mock(side_effect=[before, after]))
    if state == "failed":
        with pytest.raises(RuntimeError, match="not applied"):
            controller.taint_runner_nodes()
    else:
        controller.taint_runner_nodes()
    assert recipe.call_count == (0 if state == "empty" else 1)
    assert controller.refresh_nodes == ({"old": "runner"} if state == "survives" else {})


@pytest.mark.parametrize(
    "state", ["surviving", "deleting", "deleted", "replaced", "removed", "wrong-value", "wrong-effect"]
)
def test_refresh_taint_check_uses_uid_and_full_taint(controller, monkeypatch, caplog, state):
    controller.refresh_nodes = {"old": "runner"}
    node = refresh_node()
    if state == "deleting":
        node["metadata"]["deletionTimestamp"] = "2026-10-23T00:00:00Z"
    elif state == "replaced":
        node = refresh_node("new", tainted=False)
    elif state == "removed":
        node = refresh_node(tainted=False)
    elif state == "wrong-value":
        node["spec"]["taints"][0]["value"] = "false"
    elif state == "wrong-effect":
        node["spec"]["taints"][0]["effect"] = "PreferNoSchedule"
    kube = Mock(return_value=json.dumps({"items": [] if state == "deleted" else [node]}))
    monkeypatch.setattr(controller, "kube", kube)
    if state in ("removed", "wrong-value", "wrong-effect"):
        with pytest.raises(RuntimeError, match="Refresh taint removed"):
            controller.verify_refresh_taints()
    else:
        controller.verify_refresh_taints()
    kube.assert_called_once_with("get", "nodes", "-o", "json")
    assert ("live taint preservation was not observed" in caplog.text) == (state in ("deleted", "replaced"))


def test_refresh_taint_query_failure_is_not_recycling(controller, monkeypatch, clock):
    controller.refresh_nodes = {"old": "runner"}
    monkeypatch.setattr(controller, "kube", Mock(side_effect=subprocess.SubprocessError("unavailable")))
    with pytest.raises(TimeoutError, match=r"refresh taints preserved.*unavailable"):
        controller.verify_refresh_taints()
    assert clock[0] == 120
    assert controller.refresh_taint_failure is None


@pytest.mark.parametrize("state", ["preserved", "deleted", "replaced", "missing"])
def test_resume_retries_only_taint_query(controller, monkeypatch, clock, state):
    controller.draining = True
    node = refresh_node()
    monkeypatch.setattr(controller, "runner_nodes", lambda: [node])
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(8))
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    nodes = {
        "preserved": [node],
        "deleted": [],
        "replaced": [refresh_node("new", tainted=False)],
        "missing": [refresh_node(tainted=False)],
    }[state]

    def query(*args):
        assert controller.draining  # Limits alone must not mark resume successful.
        assert controller.resume_limits_restored
        if clock[0] == 0:
            raise subprocess.SubprocessError("unavailable")
        return json.dumps({"items": nodes})

    kube = Mock(side_effect=query)
    monkeypatch.setattr(controller, "kube", kube)
    if state == "missing":
        with pytest.raises(module.RefreshTaintError, match="Refresh taint removed"):
            controller.resume()
    else:
        controller.resume()
    assert controller.draining == (state == "missing")
    assert kube.call_count == 2
    assert clock[0] == 5
    recipe.assert_called_once_with("resume-runners", timeout=210)


@pytest.mark.parametrize("phase", ["cleanup", "run", "restore"])
def test_missing_taint_stops_resume_retries_but_cleans_resources(
    controller, monkeypatch, caplog, controller_runtime, phase
):
    controller.draining = True
    # Taint preservation failures must not block canary cleanup.
    controller.refresh_nodes = {"old": "runner"}
    monkeypatch.setattr(controller, "runner_nodes", lambda: [refresh_node(tainted=False)])
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(8))
    kube = Mock(return_value=json.dumps({"items": [refresh_node(tainted=False)]}))
    monkeypatch.setattr(controller, "kube", kube)
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    controller.canary_cleanup_needed = True
    monkeypatch.setattr(controller, "runs", list)
    api = Mock(return_value={})
    monkeypatch.setattr(controller, "api", api)
    if phase == "cleanup":
        assert not controller.cleanup()
    else:
        monkeypatch.setattr(controller, "prepare", Mock())
        monkeypatch.setattr(controller, "run", controller.resume)
        with pytest.raises(module.RefreshTaintError, match="Refresh taint removed"):
            module.run_controller(controller, restore_only=phase == "restore")
    assert controller.draining
    assert controller.resume_limits_restored
    assert controller.refresh_taint_failure is not None
    assert "Runner limits restored; taint preservation failed" in caplog.text
    assert "RECOVERY REQUIRED" not in caplog.text
    assert not controller.canary_cleanup_needed
    assert api.call_args_list == [
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
    ]
    assert not controller.cleanup()  # atexit must retain the failure without resuming again.
    recipe.assert_called_once_with("resume-runners", timeout=210)
    kube.assert_called_once()


def test_taint_query_timeout_does_not_repeat_resume_or_report_limits_recovery(controller, monkeypatch, clock, caplog):
    controller.draining = True
    controller.args.cleanup_timeout = 100
    monkeypatch.setattr(controller, "runner_nodes", lambda: [refresh_node()])
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(8))
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    kube = Mock(side_effect=subprocess.SubprocessError("unavailable"))
    monkeypatch.setattr(controller, "kube", kube)
    with pytest.raises(TimeoutError, match="refresh taints preserved"):
        controller.resume()
    assert controller.draining
    assert controller.resume_limits_restored
    assert not controller.cleanup()
    assert clock[0] == controller.cleanup_deadline == 220
    assert "Runner limits restored; refresh-taint verification incomplete" in caplog.text
    assert "RECOVERY REQUIRED" not in caplog.text
    recipe.assert_called_once_with("resume-runners", timeout=210)


def test_cleanup_can_finish_taint_query_after_timeout_without_resuming(controller, monkeypatch, clock):
    controller.draining = True
    node = refresh_node()
    monkeypatch.setattr(controller, "runner_nodes", lambda: [node])
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(8))
    recipe = Mock()
    monkeypatch.setattr(controller, "recipe", recipe)
    kube = Mock(side_effect=subprocess.SubprocessError("unavailable"))
    monkeypatch.setattr(controller, "kube", kube)
    with pytest.raises(TimeoutError, match="refresh taints preserved"):
        controller.resume()
    kube.side_effect = None
    kube.return_value = json.dumps({"items": [node]})
    assert controller.cleanup()
    assert not controller.draining
    recipe.assert_called_once_with("resume-runners", timeout=210)


def test_startup_repairs_mixed_limits_before_creating_canary(controller, monkeypatch):
    controller.desired = {"cpu": 8, "gpu": 4, "excluded": 0}
    items = [
        *ars(8),
        {"metadata": {"name": "gpu"}, "spec": {"maxRunners": 2}},
        {"metadata": {"name": "excluded"}, "spec": {"maxRunners": 3}},
    ]
    node = refresh_node()
    events = []
    monkeypatch.setattr(controller, "prepare", Mock())
    monkeypatch.setattr(controller, "scale_sets", lambda: items)
    monkeypatch.setattr(controller, "runner_nodes", lambda: [node])
    monkeypatch.setattr(controller, "kube", lambda *args: json.dumps({"items": [node]}))
    monkeypatch.setattr(controller, "capacities", lambda: {"cpu": 8, "gpu": 4, "excluded": 0})

    def recipe(name, timeout):
        assert name == "resume-runners"
        assert controller.draining
        events.append("restore")
        for item in items:
            item["spec"]["maxRunners"] = controller.desired[item["metadata"]["name"]]

    def create():
        assert module.runner_limits(items) == controller.desired
        assert controller.refresh_nodes == {"old": "runner"}
        assert not controller.draining
        events.append("canary")
        raise RuntimeError("stop after baseline verified")

    monkeypatch.setattr(controller, "recipe", recipe)
    monkeypatch.setattr(controller, "create_pr", create)
    with pytest.raises(RuntimeError, match="stop after baseline verified"):
        controller.run()
    assert events == ["restore", "canary"]


@pytest.mark.parametrize("failure", ["command", "unchanged", "missing", "unexpected"])
@pytest.mark.parametrize("defer", [False, True])
def test_startup_repair_failure_recovers_without_creating_canary(
    controller, monkeypatch, tmp_path, controller_runtime, failure, defer
):
    # A new runner has no previous attempt's file; failed startup must create its own handoff.
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(module, "PROC_ROOT", proc_root)
    controller.recovery_file = tmp_path / "attempt-2.json"
    controller.defer_recovery = defer
    monkeypatch.setattr(controller, "prepare", Mock())
    items = ars(0)
    if failure == "missing":
        controller.desired["missing"] = 4
    elif failure == "unexpected":
        items.append({"metadata": {"name": "unexpected"}, "spec": {"maxRunners": 0}})
    monkeypatch.setattr(controller, "scale_sets", lambda: items)
    monkeypatch.setattr(controller, "runner_nodes", list)
    create = Mock()
    monkeypatch.setattr(controller, "create_pr", create)
    calls = []

    def recipe(name, timeout):
        assert name == "resume-runners"
        assert controller.recovery_file.exists()
        calls.append(name)
        if len(calls) == 1 and failure == "command":
            raise subprocess.SubprocessError("repair failed")
        if len(calls) > 1 or failure != "unchanged":
            items[0]["spec"]["maxRunners"] = 8

    monkeypatch.setattr(controller, "recipe", recipe)
    error = subprocess.SubprocessError if failure == "command" else ValueError
    with pytest.raises(error):
        module.run_controller(controller)
    create.assert_not_called()
    structural_drift = failure in {"missing", "unexpected"}
    if defer:
        assert len(calls) == 1
        assert controller.draining
        assert controller.recovery_file.exists()
        controller.defer_recovery = False  # The separate restore-only process does not defer again.
        controller.skip_canary_cleanup = False
        if structural_drift:
            with pytest.raises(ValueError, match="Runner limits differ"):
                module.run_controller(controller, restore_only=True)
        else:
            module.run_controller(controller, restore_only=True)
        assert len(calls) == (4 if structural_drift else 2)
        assert controller.draining == structural_drift
    else:
        assert len(calls) == (4 if structural_drift else 2)
        assert controller.draining == structural_drift


@pytest.fixture
def release_controller(controller, monkeypatch):
    controller.pr = 42
    for name in (
        "prepare",
        "create_pr",
        "find_run",
        "inflight_ready",
        "drained",
        "completed",
        "stop_drain",
        "observe_held",
        "taint_runner_nodes",
        "resume",
        "wait_with_wakeup",
        "verify_refresh_taints",
    ):
        monkeypatch.setattr(controller, name, Mock(return_value=True))
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "capacities", lambda: {"cpu": 0})
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=Mock(wait=Mock(return_value=0))))
    return controller


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, "gh"), subprocess.TimeoutExpired("gh", 30)])
def test_inflight_release_retries_identical_patch_after_transient_failure(
    release_controller, monkeypatch, clock, error
):
    test = release_controller
    api = Mock(side_effect=[error, {}])
    monkeypatch.setattr(test, "api", api)
    with test.deadline_scope(test.args.run_timeout):
        test.run()
    patch = call("pulls/42", {"body": "OSDC_DRAIN_RELEASE_abc123"}, "PATCH")
    assert api.call_args_list == [patch, patch]
    test.completed.assert_called_once_with("inflight", 1)
    test.resume.assert_called_once_with()
    assert clock[0] == 10


@pytest.mark.parametrize(("budget", "elapsed"), [(15, 15), (4500, 180)])
def test_inflight_release_failure_stops_at_phase_or_controller_deadline(
    release_controller, monkeypatch, clock, budget, elapsed
):
    test = release_controller
    api = Mock(side_effect=subprocess.CalledProcessError(1, "gh"))
    monkeypatch.setattr(test, "api", api)
    with pytest.raises(TimeoutError, match="in-flight job released"), test.deadline_scope(budget):
        test.run()
    assert api.call_count > 1
    assert clock[0] == elapsed
    test.completed.assert_not_called()
    test.observe_held.assert_not_called()
    test.resume.assert_not_called()


def test_release_retries_share_existing_completion_budget(release_controller, monkeypatch, clock):
    test = release_controller
    api = Mock(side_effect=[subprocess.CalledProcessError(1, "gh"), {}])
    monkeypatch.setattr(test, "api", api)
    test.completed.return_value = False
    with pytest.raises(TimeoutError, match="in-flight job succeeds"), test.deadline_scope(test.args.run_timeout):
        test.run()
    assert api.call_count == 2
    assert clock[0] == 180
    test.resume.assert_not_called()


@pytest.mark.parametrize(
    ("handoff", "initial_limit", "elapsed"),
    [
        # Cover initial limit drift without multiplying it by every timing case.
        (False, 0, 0),
        (False, 3, 0),
        (False, 8, 0),
        (False, 12, 0),
        (False, None, 0),
        # Handoff must work with both restored and already-correct limits.
        (True, 0, 0),
        (True, 8, 0),
        (False, 8, 123.25),
        (False, 8, 4499.75),
    ],
)
def test_controller_orders_drain_release_observe_and_resume(
    controller, monkeypatch, clock, elapsed, handoff, initial_limit
):
    monkeypatch.setenv("AWS_SESSION_TOKEN", "drain-session-token")
    for name in ("OSDC_TAINT_NODES", "OSDC_UNTAINT_NODES", "OSDC_DRAIN_TIMEOUT_SECS"):
        monkeypatch.setenv(name, "ambient-value")
    events = []
    if handoff:
        proc_root = controller.args.root_dir / "proc"
        proc_root.mkdir()
        monkeypatch.setattr(module, "PROC_ROOT", proc_root)
        controller.recovery_file = controller.args.root_dir / "intent.json"
    record = Mock(wraps=controller.record_recovery)
    monkeypatch.setattr(controller, "record_recovery", record)
    cleanup = Mock(side_effect=lambda: events.append("cleanup_stale_canary"))
    monkeypatch.setattr(controller, "cleanup_stale_canary", cleanup)
    for name in ("prepare", "create_pr", "stop_drain", "observe_held", "taint_runner_nodes", "verify_refresh_taints"):
        monkeypatch.setattr(controller, name, lambda name=name: events.append(name))
    items = ars(initial_limit)
    monkeypatch.setattr(controller, "scale_sets", lambda: items)
    monkeypatch.setattr(controller, "capacities", lambda: {"cpu": 0})
    for name in ("find_run", "inflight_ready", "held_ready", "drained"):
        monkeypatch.setattr(controller, name, lambda name=name: events.append(name) or True)
    monkeypatch.setattr(controller, "completed", lambda phase, count: events.append(f"complete-{phase}") or True)
    monkeypatch.setattr(controller, "held_completed", lambda: events.append("complete-held") or True)

    def resume():
        assert controller.draining
        assert record.call_count == 1
        if handoff:
            assert controller.recovery_file.exists()
        events.append("resume")
        items[0]["spec"]["maxRunners"] = 8
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "wait_for", lambda description, check, timeout, **kwargs: check())
    api = Mock(side_effect=lambda *a, **kw: events.append("release") or {})
    monkeypatch.setattr(module, "api", api)
    proc = Mock()
    proc.wait.return_value = 0

    def launch(*args, **kwargs):
        if handoff:
            state = json.loads(controller.recovery_file.read_text())
            assert kwargs["env"][module.DRAIN_OWNER_ENV] == state["token"]
        return proc

    popen = Mock(side_effect=launch)
    monkeypatch.setattr(module.subprocess, "Popen", popen)

    def wait_with_wakeup(description, phase, check, timeout):
        assert phase == "held"
        assert "resume" in events
        return check()

    monkeypatch.setattr(controller, "wait_with_wakeup", wait_with_wakeup)
    with controller.deadline_scope(controller.args.run_timeout):
        clock[0] = elapsed
        controller.run()
    assert events == [
        "prepare",
        "cleanup_stale_canary",
        *(["resume"] if initial_limit != 8 else []),
        "create_pr",
        "find_run",
        "inflight_ready",
        "drained",
        "release",
        "complete-inflight",
        "stop_drain",
        "observe_held",
        "taint_runner_nodes",
        "resume",
        "complete-held",
        "verify_refresh_taints",
    ]
    cleanup.assert_called_once_with()
    assert not controller.draining
    record.assert_called_once_with()
    popen.assert_called_once()
    assert popen.call_args.args[0] == ["just", "drain-runners", CLUSTER]
    assert popen.call_args.kwargs["start_new_session"] is True
    assert popen.call_args.kwargs["env"]["AWS_SESSION_TOKEN"] == os.environ["AWS_SESSION_TOKEN"]
    assert popen.call_args.kwargs["env"]["OSDC_TAINT_NODES"] == "no"
    assert popen.call_args.kwargs["env"]["OSDC_UNTAINT_NODES"] == "no"
    assert popen.call_args.kwargs["env"]["OSDC_DRAIN_TIMEOUT_SECS"] == "900"
    proc.wait.assert_called_once_with(timeout=min(900, controller.args.run_timeout - elapsed))
    assert api.call_args.args[1] == {"body": "OSDC_DRAIN_RELEASE_abc123"}


@pytest.mark.parametrize(
    ("pod_lifetime", "run_budget", "expected_time", "times_out"),
    [
        (340, 4500, 440, False),
        # Pod teardown exceeds the old 180-second wait after probe success.
        (600, 4500, 700, False),
        (899, 4500, 999, False),
        (901, 4500, 1000, True),
        (float("inf"), 4500, 1000, True),
        # The earlier main deadline still wins over the drain-phase deadline.
        (600, 650, 650, True),
    ],
)
def test_drain_recipe_covers_slow_probe_teardown_but_still_bounds_pod_wait(
    controller, monkeypatch, clock, pod_lifetime, run_budget, expected_time, times_out
):
    controller.args.drain_timeout = 300
    launched = []
    released = []
    for name in (
        "create_pr",
        "find_run",
        "inflight_ready",
        "wait_with_wakeup",
        "stop_drain",
        "observe_held",
        "taint_runner_nodes",
        "verify_refresh_taints",
        "resume",
    ):
        monkeypatch.setattr(controller, name, Mock(return_value=True))
    monkeypatch.setattr(controller, "prepare", lambda: module.time.sleep(100))
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(0 if launched else 8))
    monkeypatch.setattr(
        controller, "capacities", lambda: {"cpu": 0 if launched and clock[0] >= launched[0] + 280 else 8}
    )
    monkeypatch.setattr(controller, "api", lambda *args: released.append(clock[0]))
    monkeypatch.setattr(
        controller,
        "probes",
        lambda phase, count: [job(status="completed" if clock[0] >= released[0] + 40 else "in_progress")],
    )

    def launch(*args, **kwargs):
        launched.append(clock[0])
        recipe_deadline = clock[0] + int(kwargs["env"]["OSDC_DRAIN_TIMEOUT_SECS"])
        pod_gone = clock[0] + pod_lifetime

        def wait(timeout):
            # Recipe polling/teardown may finish after its internal deadline.
            finished = min(recipe_deadline + 30, pod_gone)
            if finished > clock[0] + timeout:
                module.time.sleep(timeout)
                raise subprocess.TimeoutExpired("just drain-runners", timeout)
            clock[0] = max(clock[0], finished)
            return int(recipe_deadline < pod_gone)

        return Mock(wait=Mock(side_effect=wait))

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    with controller.deadline_scope(run_budget):
        if times_out:
            with pytest.raises(subprocess.TimeoutExpired):
                controller.run()
        else:
            controller.run()
    assert launched == [100]
    assert released == [380]
    assert clock[0] == expected_time
    assert controller.resume.call_count == (0 if times_out else 1)


def test_drain_phase_deadline_bounds_probe_completion(release_controller, monkeypatch, clock):
    test = release_controller
    test.args.drain_timeout = 1000
    monkeypatch.setattr(test, "api", Mock(return_value={}))
    monkeypatch.setattr(test, "drained", lambda: clock[0] >= 800)
    test.completed.return_value = False
    with pytest.raises(TimeoutError, match="in-flight job succeeds"), test.deadline_scope(test.args.run_timeout):
        test.run()
    assert clock[0] == 900
    test.drain.wait.assert_not_called()
    test.resume.assert_not_called()


def test_drain_phase_deadline_does_not_bound_observation_or_resume(release_controller, monkeypatch, clock):
    test = release_controller
    monkeypatch.setattr(test, "api", Mock(return_value={}))
    monkeypatch.setattr(test, "observe_held", lambda: module.time.sleep(901))
    with test.deadline_scope(test.args.run_timeout):
        test.run()
        assert test.deadline == test.args.run_timeout
    assert clock[0] == 901
    test.resume.assert_called_once_with()
    test.verify_refresh_taints.assert_called_once_with()


def test_nonzero_recipe_exit_is_failure(controller, monkeypatch):
    for name in ("prepare", "create_pr"):
        monkeypatch.setattr(controller, name, Mock())
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "capacities", Mock())

    def wait(description, check, timeout, **kwargs):
        if description == "initial runner scale sets available":
            check()

    monkeypatch.setattr(controller, "wait_for", wait)
    monkeypatch.setattr(module, "api", Mock())
    proc = Mock()
    proc.wait.return_value = 1
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    with pytest.raises(RuntimeError, match="drain-runners failed"):
        controller.run()
    assert controller.draining


def test_drain_cannot_pass_without_zero_limits(controller, monkeypatch, clock):
    for name in ("prepare", "create_pr", "find_run", "inflight_ready"):
        monkeypatch.setattr(controller, name, Mock(return_value=True))
    monkeypatch.setattr(controller, "scale_sets", lambda: ars(8))
    monkeypatch.setattr(controller, "capacities", Mock(return_value={"cpu": 0}))
    popen = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    actual_wait = controller.wait_for

    def wait(description, check, timeout, **kwargs):
        if description.startswith("zero runner"):
            return actual_wait(description, check, 1)
        return check()

    monkeypatch.setattr(controller, "wait_for", wait)
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
        ("  123\tZ+  \n 456   S \n", False),
    ],
)
def test_process_group_running_ignores_other_groups_and_zombies(monkeypatch, snapshot, expected):
    command = Mock(return_value=subprocess.CompletedProcess([], 0, snapshot, ""))
    monkeypatch.setattr(module, "command", command)
    assert module.process_group_running(123) is expected
    command.assert_called_once_with(["ps", "-eo", "pgid=,stat="], deadline=None, timeout=2)


@pytest.mark.parametrize("malformed", ["", "   ", "456", "456 S extra", "invalid S"])
@pytest.mark.parametrize("live_first", [False, True])
def test_process_group_running_finds_live_writer_despite_malformed_rows(monkeypatch, malformed, live_first):
    rows = ["123 S", malformed] if live_first else [malformed, "123 S"]
    command = Mock(return_value=Mock(stdout="\n".join([*rows, ""])))
    monkeypatch.setattr(module, "command", command)
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    assert module.process_group_running(123)
    kill.assert_not_called()


@pytest.mark.parametrize("malformed", ["", "   ", "456", "456 S extra", "invalid S"])
@pytest.mark.parametrize("group_status", ["absent", "present", "denied"])
def test_process_group_running_requires_confirmation_after_malformed_rows(monkeypatch, malformed, group_status):
    command = Mock(return_value=Mock(stdout=f"123 Z+\n{malformed}\n456 S\n"))
    monkeypatch.setattr(module, "command", command)
    error = {"absent": ProcessLookupError(), "present": None, "denied": PermissionError()}[group_status]
    kill = Mock(side_effect=error)
    monkeypatch.setattr(module.os, "killpg", kill)
    if group_status == "absent":
        assert not module.process_group_running(123)
    else:
        reason = "still exists" if group_status == "present" else "cannot inspect"
        with pytest.raises(ValueError, match=f"Malformed ps output; .*{reason}"):
            module.process_group_running(123)
    kill.assert_called_once_with(123, 0)


@pytest.mark.parametrize("group_absent", [False, True])
def test_stop_drain_handles_malformed_snapshot_without_unnecessary_kill(controller, monkeypatch, clock, group_absent):
    proc = Mock(pid=123)
    controller.drain = proc
    command = Mock(side_effect=[Mock(stdout="123 Z\nmalformed\n"), Mock(stdout="123 Z\n456 S\n")])
    monkeypatch.setattr(module, "command", command)

    def kill_group(pgid, sig):
        if sig == 0 and group_absent:
            raise ProcessLookupError

    kill = Mock(side_effect=kill_group)
    monkeypatch.setattr(module.os, "killpg", kill)
    controller.stop_drain()
    assert kill.call_args_list == [call(123, signal.SIGTERM), call(123, 0)]
    assert command.call_count == (1 if group_absent else 2)
    assert clock[0] == (0 if group_absent else 0.5)
    proc.wait.assert_called_once_with(timeout=2)
    assert controller.drain is None


def test_cleanup_refuses_resume_when_malformed_snapshot_never_resolves(controller, monkeypatch, clock):
    controller.drain = Mock(pid=123)
    controller.draining = True
    monkeypatch.setattr(module, "command", Mock(return_value=Mock(stdout="123 Z\nmalformed\n")))
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    resume = Mock()
    monkeypatch.setattr(controller, "resume", resume)
    assert not controller.cleanup()
    assert call(123, signal.SIGKILL) in kill.call_args_list
    resume.assert_not_called()
    assert controller.drain is not None
    assert controller.draining


@pytest.mark.parametrize("force_kill", [False, True])
def test_stop_drain_kills_owned_process_group(controller, monkeypatch, clock, force_kill):
    proc = Mock(pid=123)
    controller.drain = proc
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    running = Mock(side_effect=lambda pgid, deadline=None: force_kill and clock[0] < 2)
    monkeypatch.setattr(module, "process_group_running", running)
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


def test_cleanup_refuses_resume_if_process_group_survives(controller, monkeypatch, clock):
    controller.drain = Mock(pid=123)
    controller.draining = True
    kill = Mock()
    resume = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(module, "process_group_running", Mock(return_value=True))
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
    controller.draining = controller.canary_cleanup_needed = True

    def resume():
        events.append("resume")
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "runs", lambda: [canary_run(1, "queued"), canary_run(2, "completed")])

    def api_side_effect(*a, **kw):
        events.append(a[0])
        if a[0].startswith("pulls?"):
            return [canary_pr(3)]
        return {}

    api = Mock(side_effect=api_side_effect)
    monkeypatch.setattr(module, "api", api)
    assert controller.cleanup()
    assert events == [
        "resume",
        f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100",
        "actions/runs/1/cancel",
        "pulls/3",
        f"git/refs/heads/{controller.branch}",
    ]
    assert not controller.canary_cleanup_needed
    assert controller.cleanup()
    assert len(events) == 5


def test_cleanup_missing_branch_preserves_success_and_unregisters_atexit(controller, monkeypatch, controller_runtime):
    controller.canary_cleanup_needed = True
    monkeypatch.setattr(controller, "run", Mock())
    monkeypatch.setattr(controller, "runs", lambda: [canary_run(1, "completed")])
    api = Mock(
        side_effect=[
            [canary_pr(3)],  # One open PR on the branch.
            {},  # Closing the PR succeeds before the missing branch is detected.
            subprocess.CalledProcessError(1, "gh", stderr="Reference does not exist (HTTP 422)"),
            [],
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    module.run_controller(controller)
    assert not controller.canary_cleanup_needed
    module.atexit.unregister.assert_called_once_with(controller.cleanup)
    assert controller.cleanup()
    assert api.call_args_list == [
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100"),
        call("pulls/3", {"state": "closed"}, "PATCH"),
        call(f"git/refs/heads/{controller.branch}", method="DELETE"),
        call(f"git/matching-refs/heads/{controller.branch}"),
    ]


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_cleanup_keeps_canary_cleanup_intent_when_delete_is_unconfirmed(controller, monkeypatch, lookup_fails):
    controller.canary_cleanup_needed = True
    monkeypatch.setattr(controller, "runs", list)
    monkeypatch.setattr(module, "command", Mock(return_value=subprocess.CompletedProcess([], 0, "[]", "")))
    api = Mock(
        side_effect=[
            subprocess.CalledProcessError(1, "gh", stderr="Deletion failed (HTTP 422)"),
            subprocess.CalledProcessError(1, "gh", stderr="Lookup failed (HTTP 403)")
            if lookup_fails
            else [{"ref": f"refs/heads/{controller.branch}"}],
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    assert not controller.cleanup()
    assert controller.canary_cleanup_needed


def test_cleanup_retries_restore_and_keeps_failed_state(controller, monkeypatch, caplog):
    controller.draining = True
    resume = Mock(side_effect=ValueError("not restored"))
    monkeypatch.setattr(controller, "resume", resume)
    assert not controller.cleanup()
    assert resume.call_count == 3
    assert controller.draining
    assert "RECOVERY REQUIRED" in caplog.text
    assert not controller.cleanup()  # atexit cannot start another batch of retries.
    assert resume.call_count == 3


def test_cleanup_recovers_after_transient_error(controller, monkeypatch):
    controller.draining = True
    resume = Mock()
    monkeypatch.setattr(controller, "resume", resume)

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
    controller.draining = controller.canary_cleanup_needed = True

    def resume():
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "runs", Mock(side_effect=subprocess.SubprocessError("GitHub unavailable")))
    assert not controller.cleanup()
    assert not controller.draining
    assert controller.canary_cleanup_needed
    monkeypatch.setattr(controller, "stop_drain", Mock(side_effect=OSError("cannot stop")))
    assert not controller.cleanup()


def test_positive_and_cli_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER])
    args = module.parse_args()
    assert args.start_timeout == args.finish_timeout == 1800
    assert args.drain_timeout == 300
    assert args.run_timeout == 4500
    assert args.cleanup_timeout == 600
    assert args.restore_timeout == 600
    assert module.OBSERVE_TIMEOUT == 120
    assert not args.defer_recovery
    assert args.root_dir == module.UPSTREAM
    assert module.positive("3") == 3
    with pytest.raises(argparse.ArgumentTypeError, match="positive"):
        module.positive("0")


@pytest.mark.parametrize("flag", ["--observe-seconds", "--observe-timeout"])
def test_cli_has_no_observation_timeout_option(monkeypatch, capsys, flag):
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER, flag, "60"])
    with pytest.raises(SystemExit) as exc:
        module.parse_args()
    assert exc.value.code == 2
    assert f"unrecognized arguments: {flag} 60" in capsys.readouterr().err


def test_cli_rejects_removed_clusters_yaml(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER, "--clusters-yaml", "copy.yaml"])
    with pytest.raises(SystemExit) as exc:
        module.parse_args()
    assert exc.value.code == 2
    assert "unrecognized arguments: --clusters-yaml copy.yaml" in capsys.readouterr().err


@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--restore-only"]])
@pytest.mark.parametrize("missing_config", [False, True])
def test_cli_reads_only_root_config(controller, monkeypatch, capsys, ci_environment, mode, missing_config):
    root = controller.args.root_dir
    upstream = root / "upstream"
    upstream.mkdir()
    # A conflicting ambient path and upstream config must not override root-dir.
    (upstream / "clusters.yaml").write_text("clusters: {}\n")
    monkeypatch.setenv("CLUSTERS_YAML", str(upstream / "clusters.yaml"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["test", "--cluster-id", CLUSTER, "--root-dir", str(root), "--upstream-dir", str(upstream), *mode],
    )
    run = Mock()
    monkeypatch.setattr(module, "run_controller", run)
    monkeypatch.setattr(module, "command", Mock(side_effect=AssertionError("external command")))
    if missing_config:
        (root / "clusters.yaml").unlink()
        with pytest.raises(FileNotFoundError):
            module.main()
        run.assert_not_called()
    else:
        module.main()
        if "--dry-run" in mode:
            workflow = yaml.safe_load(capsys.readouterr().out)
            for probe in workflow["jobs"].values():
                assert probe["runs-on"] == {"group": CLUSTER, "labels": ["l-x86iamx-8-16"]}
            run.assert_not_called()
        else:
            target = run.call_args.args[0]
            assert target.args.root_dir == root
            assert target.env_overrides["CLUSTERS_YAML"] == str(root / "clusters.yaml")
            assert run.call_args.kwargs["restore_only"] == ("--restore-only" in mode)


@pytest.mark.parametrize(
    "extra",
    [
        ["--cluster-id", CLUSTER, "--start-timeout", "0"],
        ["--cluster-id", CLUSTER, "--run-timeout", "0"],
        ["--cluster-id", CLUSTER, "--cleanup-timeout", "0"],
        ["--cluster-id", CLUSTER, "--restore-timeout", "0"],
        ["--cluster-id", CLUSTER, "--dry-run", "--restore-only"],
        ["--cluster-id", CLUSTER, "--defer-recovery"],
        ["--cluster-id", CLUSTER, "--defer-recovery", "--restore-only"],
        ["--cluster-id", CLUSTER, "--defer-recovery", "--dry-run"],
    ],
)
def test_cli_rejects_invalid_modes(monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["test", *extra])
    with pytest.raises(SystemExit):
        module.parse_args()


def test_signal_exits_with_original_status():
    with pytest.raises(SystemExit) as exc:
        module.on_signal(signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM


@pytest.mark.parametrize("prefix", ["renamed-", ""])
def test_dry_run_has_no_external_effects(controller, monkeypatch, capsys, ci_environment, prefix):
    for name in ci_environment:
        monkeypatch.delenv(name)
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "clusters": {
                    CLUSTER: {
                        "arc-runners": {
                            "runner_name_prefix": prefix,
                            "runner_group": CLUSTER,
                            "github_config_url": ORG_URL,
                        }
                    }
                }
            }
        )
    )
    controller.args.dry_run = True
    monkeypatch.setattr(module, "parse_args", lambda: controller.args)
    constructor = Mock()
    monkeypatch.setattr(module, "DrainTest", constructor)
    monkeypatch.setattr(module, "command", Mock(side_effect=AssertionError("external command")))
    module.main()
    content = capsys.readouterr().out
    assert "OSDC Drain Test" in content
    for job in yaml.safe_load(content)["jobs"].values():
        assert job["runs-on"]["labels"] == [f"{prefix}l-x86iamx-8-16"]
    constructor.assert_not_called()


@pytest.mark.parametrize("mode", [[], ["--restore-only"], ["--defer-recovery"]])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OSDC_DRAIN_TEST_LIVE", None),
        ("OSDC_DRAIN_TEST_LIVE", "false"),
        ("OSDC_DRAIN_TEST_LIVE", "yes"),
    ],
)
def test_live_cli_rejects_unprotected_execution_before_side_effects(
    monkeypatch, tmp_path, ci_environment, mode, name, value
):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        sys, "argv", ["test", "--cluster-id", CLUSTER, "--recovery-file", str(tmp_path / "intent.json"), *mode]
    )
    operations = [Mock() for _ in range(6)]
    config, command, constructor, controller, signals, register = operations
    monkeypatch.setattr(module, "load_cluster_config", config)
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "DrainTest", constructor)
    monkeypatch.setattr(module, "run_controller", controller)
    monkeypatch.setattr(module.signal, "signal", signals)
    monkeypatch.setattr(module.atexit, "register", register)
    with pytest.raises(SystemExit, match="local execution supports --dry-run only"):
        module.main()
    for operation in operations:
        operation.assert_not_called()
    assert not (tmp_path / "intent.json").exists()


@pytest.mark.parametrize("mode", [[], ["--restore-only"], ["--defer-recovery"]])
@pytest.mark.parametrize("cluster", [CLUSTER, "meta-staging-aws-ue1"])
def test_live_cli_accepts_ci_live_flag(monkeypatch, tmp_path, ci_environment, mode, cluster):
    monkeypatch.setattr(
        sys, "argv", ["test", "--cluster-id", cluster, "--recovery-file", str(tmp_path / "intent.json"), *mode]
    )
    constructor, controller = Mock(), Mock()
    monkeypatch.setattr(module, "DrainTest", constructor)
    monkeypatch.setattr(module, "run_controller", controller)
    module.main()
    constructor.assert_called_once()
    assert constructor.call_args.args[0].cluster_id == cluster
    controller.assert_called_once_with(constructor.return_value, restore_only=mode == ["--restore-only"])


@pytest.mark.parametrize("mode", ["run", "restore", "failure", "signal", "cleanup-failure", "cleanup-exception"])
def test_main_always_cleans_up_and_restores_signal_handlers(controller, monkeypatch, ci_environment, mode):
    controller.args.restore_only = mode == "restore"
    monkeypatch.setattr(module, "parse_args", lambda: controller.args)
    monkeypatch.setattr(module, "DrainTest", lambda *a: controller)
    for name in ("prepare", "resume", "run"):
        monkeypatch.setattr(controller, name, Mock())
    if mode == "failure":
        controller.run.side_effect = RuntimeError("test failed")
    if mode == "signal":
        controller.run.side_effect = SystemExit(130)
    if mode == "cleanup-exception":
        cleanup = Mock(side_effect=RuntimeError("cleanup failed"))
    else:
        cleanup = Mock(return_value=mode != "cleanup-failure")
    monkeypatch.setattr(controller, "cleanup", cleanup)
    signals = Mock(return_value="previous")
    monkeypatch.setattr(module.signal, "signal", signals)
    register = Mock()
    unregister = Mock()
    monkeypatch.setattr(module.atexit, "register", register)
    monkeypatch.setattr(module.atexit, "unregister", unregister)
    if mode in {"failure", "cleanup-failure", "cleanup-exception"}:
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
    unregister.assert_called_once_with(cleanup)
    assert call(signal.SIGTERM, signal.SIG_IGN) in signals.call_args_list
    assert call(signal.SIGTERM, "previous") in signals.call_args_list
    if mode == "restore":
        controller.prepare.assert_called_once()
        controller.resume.assert_called_once()


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    return elapsed


def test_nested_deadlines_only_shorten_and_reset(controller, clock):
    with controller.deadline_scope(10):
        assert controller.remaining(30) == 10
        clock[0] = 2
        with controller.deadline_scope(50):
            assert controller.remaining(30) == 8
        with controller.deadline_scope(4):
            assert controller.remaining(30) == 2
        assert controller.remaining(30) == 8
    assert controller.remaining(30) == 30
    with pytest.raises(TimeoutError, match="deadline exceeded"), controller.deadline_scope(1):
        pytest.fail("Expired scope must not execute")
    assert controller.remaining(30) == 30


def test_wait_rejects_success_returned_after_deadline(controller, clock):
    def slow_check():
        clock[0] += 6
        return True

    with pytest.raises(TimeoutError, match="late success: deadline exceeded"):
        controller.wait_for("late success", slow_check, 5)


def test_wait_clamps_api_call_to_both_phase_and_total_budget(controller, monkeypatch, clock):
    proc = Mock(returncode=0)
    calls = []

    def communicate(**kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("gh", kwargs["timeout"])
        return "", ""

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(TimeoutError, match="deadline exceeded"), controller.deadline_scope(7):
        controller.wait_for("API", lambda: controller.api("actions/runs"), 20)
    assert calls == [7, 1]  # Teardown is clamped to the budget but always gets at least 1s to drain.
    kill.assert_called_once_with(proc.pid, signal.SIGKILL)


def test_command_closes_pipes_when_group_cannot_drain(monkeypatch):
    proc = Mock()
    proc.communicate.side_effect = subprocess.TimeoutExpired("recipe", 1)
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    monkeypatch.setattr(module.os, "killpg", Mock(side_effect=ProcessLookupError))
    with pytest.raises(subprocess.TimeoutExpired):
        module.command(["recipe"], timeout=1)
    proc.stdout.close.assert_called_once()
    proc.stderr.close.assert_called_once()


@pytest.mark.parametrize("reap_times_out", [False, True])
@pytest.mark.parametrize("mode", ["deadline-between-polls", "poll-timeout", "command-timeout"])
def test_command_timeout_keeps_reaped_diagnostics(monkeypatch, clock, reap_times_out, mode):
    args = ["kubectl", "get", "pods"]
    proc = Mock(pid=123)
    kill = Mock()
    stdout, stderr = "pods pending\n", "API unavailable\n"

    def communicate(**kwargs):
        if kill.called:
            assert kwargs["timeout"] == 1
            if reap_times_out:
                raise subprocess.TimeoutExpired(args, 1, output=stdout.encode(), stderr=stderr.encode())
            return stdout, stderr
        if mode == "deadline-between-polls":
            # The first poll times out; the absolute deadline expires before the
            # next communicate() call, so remaining() raises a built-in TimeoutError
            # that command() promotes to subprocess.TimeoutExpired.
            clock[0] = 0.5
        else:
            clock[0] = 1
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"pods", stderr=b"API")

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    monkeypatch.setattr(module.os, "killpg", kill)
    if mode == "deadline-between-polls":
        monotonic = module.time.monotonic

        def now():
            result = monotonic()
            if result == 0.5:
                clock[0] = 1
            return result

        monkeypatch.setattr(module.time, "monotonic", now)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        module.command(args, deadline=1, timeout=1, cancel_event=None if mode == "command-timeout" else Event())
    assert caught.value.__notes__ == [f"Command: {args!r}\nstdout: {stdout}\nstderr: {stderr}"]
    assert caught.value.cmd == args
    assert caught.value.output == (stdout.encode() if reap_times_out else stdout)
    assert caught.value.stderr == (stderr.encode() if reap_times_out else stderr)
    assert proc.communicate.call_count == 2
    assert proc.stdout.close.call_count == int(reap_times_out)
    assert proc.stderr.close.call_count == int(reap_times_out)
    kill.assert_called_once_with(123, signal.SIGKILL)


def test_cancellable_command_deadline_between_polls_preserves_partial_output(monkeypatch, clock):
    """A TimeoutError from remaining() carries partial output from earlier polls."""
    args = ["kubectl", "get", "pods"]
    proc = Mock(pid=123)
    kill = Mock()

    def communicate(**kwargs):
        if kill.called:
            # Reap returns nothing; the partial output from the first poll must survive.
            return "", ""
        clock[0] = 0.5
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=b"pods", stderr=b"API")

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))
    monkeypatch.setattr(module.os, "killpg", kill)
    monotonic = module.time.monotonic

    def now():
        result = monotonic()
        if result == 0.5:
            clock[0] = 1
        return result

    monkeypatch.setattr(module.time, "monotonic", now)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        module.command(args, deadline=1, timeout=1, cancel_event=Event())

    assert caught.value.cmd == args
    assert caught.value.output == b"pods"
    assert caught.value.stderr == b"API"
    assert isinstance(caught.value.__cause__, TimeoutError)
    kill.assert_called_once_with(123, signal.SIGKILL)


@pytest.mark.parametrize("startup_delay", [0, 1.5], ids=["immediate", "slow-start"])
def test_command_timeout_kills_real_recipe_descendants(controller, tmp_path, monkeypatch, startup_delay):
    marker = tmp_path / "child-pgid"
    child = (
        "import os, pathlib, sys, time; "
        f"time.sleep({startup_delay}); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpgrp())); time.sleep(30)"
    )
    parent = "import subprocess, sys; subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]], check=True)"
    command_args = [sys.executable, "-c", parent, child, str(marker)]
    real_popen = subprocess.Popen
    owned = None

    def descendant_ready():
        try:
            return int(marker.read_text()) == owned.pid
        except (FileNotFoundError, ValueError):
            return False

    def launch(args, **kwargs):
        nonlocal owned
        if args != command_args:
            return real_popen(args, **kwargs)
        assert kwargs["start_new_session"] is True
        owned = real_popen(args, **kwargs)
        # Separate process startup from the communicate() timeout being tested.
        controller.wait_for("recipe descendant ready", descendant_ready, 10, poll=0.01)
        return owned

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            module.command(command_args, timeout=1)
        assert exc.value.timeout == 1
        assert owned is not None
        controller.wait_for(
            "recipe descendants stopped",
            lambda: not module.process_group_running(owned.pid, controller.deadline),
            2,
            poll=0.05,
        )
    finally:
        if owned is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(owned.pid, signal.SIGKILL)
            owned.communicate(timeout=5)


def test_command_cancellation_kills_real_descendants(controller, tmp_path):
    marker = tmp_path / "cancel-child-pgid"
    child = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpgrp())); time.sleep(30)"
    parent = "import subprocess, sys; subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]], check=True)"
    cancelled = Event()
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        module.command, [sys.executable, "-c", parent, child, str(marker)], timeout=5, cancel_event=cancelled
    )
    try:
        controller.wait_for("descendant started", marker.exists, 3, poll=0.01)
        cancelled.set()
        with pytest.raises(CancelledError):
            future.result(timeout=3)
        pgid = int(marker.read_text())
        controller.wait_for(
            "cancelled descendants stopped", lambda: not module.process_group_running(pgid), 2, poll=0.01
        )
    finally:
        cancelled.set()
        if marker.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(marker.read_text()), signal.SIGKILL)
        pool.shutdown(wait=True, cancel_futures=True)


def test_observation_retry_sleep_cannot_extend_main_budget(observing_controller, clock):
    observing_controller.capacities.side_effect = ValueError("Missing ready listeners")
    with pytest.raises(TimeoutError, match="deadline exceeded"), observing_controller.deadline_scope(7):
        observing_controller.observe_held()
    assert clock[0] == 7


@pytest.fixture
def controller_runtime(monkeypatch):
    # Failure tests must not register real exit callbacks that can contact staging.
    monkeypatch.setattr(module.atexit, "register", Mock())
    monkeypatch.setattr(module.atexit, "unregister", Mock())
    monkeypatch.setattr(module.signal, "signal", Mock(return_value=signal.SIG_DFL))


def test_main_timeout_still_gets_independent_cleanup_budget(controller, monkeypatch, clock, controller_runtime):
    controller.args.run_timeout = 5
    controller.args.cleanup_timeout = 10

    def run():
        controller.draining = True
        controller.wait_for("main flow", lambda: False, 100)

    def resume():
        assert controller.remaining(100) == 10
        clock[0] += 2
        controller.draining = False

    monkeypatch.setattr(controller, "run", run)
    monkeypatch.setattr(controller, "resume", resume)
    with pytest.raises(TimeoutError, match="main flow"):
        module.run_controller(controller)
    assert clock[0] == 7
    assert controller.cleanup_deadline == 15
    assert not controller.draining
    assert controller.remaining(100) == 100


@pytest.mark.parametrize(
    ("command_seconds", "run_budget", "expected_timeouts"),
    [(2, 5, [5, 3, 1]), (31, 50, [30])],
)
def test_startup_cleanup_commands_share_main_budget(
    controller, monkeypatch, clock, controller_runtime, command_seconds, run_budget, expected_timeouts
):
    controller.args.run_timeout = run_budget
    monkeypatch.delattr(controller, "cleanup_stale_canary")
    prepare = Mock()
    create = Mock()
    scale_sets = Mock()
    monkeypatch.setattr(controller, "prepare", prepare)
    monkeypatch.setattr(controller, "create_pr", create)
    monkeypatch.setattr(controller, "scale_sets", scale_sets)
    default_executor = Mock(side_effect=AssertionError("Shared executor must not bypass the deadline"))
    monkeypatch.setattr("phases.run_cmd", default_executor)
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    timeouts = []
    processes = []

    def popen(args, **kwargs):
        assert args[0] == "gh"
        assert kwargs["start_new_session"] is True
        proc = Mock(returncode=0)
        processes.append(proc)
        timed_out = False

        def communicate(**options):
            nonlocal timed_out
            if timed_out:  # Reaping the killed command must not start another wait.
                assert options["timeout"] <= 2
                return "", ""
            timeout = options["timeout"]
            timeouts.append(timeout)
            clock[0] += min(command_seconds, timeout)
            if command_seconds > timeout:
                timed_out = True
                raise subprocess.TimeoutExpired(args, timeout)
            if "actions/runs?" in args[2]:
                return json.dumps({"workflow_runs": [canary_run(10, "queued")]}), ""
            return "[]", ""

        proc.communicate.side_effect = communicate
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    # A cancellation timing out can exhaust the shared deadline before its status recheck.
    with pytest.raises((subprocess.TimeoutExpired, TimeoutError)):
        module.run_controller(controller)
    assert timeouts == expected_timeouts
    assert clock[0] == min(run_budget, 30)
    assert controller.cleanup_deadline == clock[0] + controller.args.cleanup_timeout
    assert controller.remaining(100) == 100
    kill.assert_called_once_with(processes[-1].pid, signal.SIGKILL)
    prepare.assert_called_once()
    default_executor.assert_not_called()
    scale_sets.assert_not_called()
    create.assert_not_called()
    assert not controller.draining
    assert controller.drain is None


def test_cleanup_and_atexit_share_deadline(controller, monkeypatch, clock, caplog):
    controller.args.cleanup_timeout = 5
    controller.draining = True
    attempts = []

    def resume():
        attempts.append(controller.remaining(4))
        clock[0] += attempts[-1]
        raise ValueError("still drained")

    monkeypatch.setattr(controller, "resume", resume)
    assert not controller.cleanup()
    assert attempts == [4, 1]
    assert clock[0] == controller.cleanup_deadline == 5
    assert not controller.cleanup()
    assert attempts == [4, 1]
    assert "RECOVERY REQUIRED" in caplog.text


def test_restore_only_three_slow_attempts_share_total_budget(controller, monkeypatch, clock, controller_runtime):
    controller.args.restore_timeout = 900  # Explicit larger budget to exercise all three slow attempts.
    timeouts = []
    proc = Mock(returncode=0)

    def communicate(**kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        return "", ""

    proc.communicate.side_effect = communicate
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=proc))

    def prepare():
        module.command(["prepare"], timeout=120)
        module.command(["map"], timeout=90)

    def resume():
        module.command(["resume"], timeout=120)
        module.command(["limits"], timeout=30)
        module.command(["taints"], timeout=30)
        raise ValueError("not restored")

    monkeypatch.setattr(controller, "prepare", prepare)
    monkeypatch.setattr(controller, "resume", resume)
    with pytest.raises(ValueError, match="not restored"):
        module.run_controller(controller, restore_only=True)
    assert clock[0] == 750
    assert controller.cleanup_deadline == 900
    assert controller.recovery_attempts == 3
    assert not controller.cleanup()  # atexit cannot acquire three new attempts.
    assert clock[0] == 750
    assert timeouts == [120, 90] + [120, 30, 30] * 3


def test_restore_deadline_includes_prepare_and_retries(controller, monkeypatch, clock, controller_runtime):
    controller.args.restore_timeout = 8
    monkeypatch.setattr(controller, "prepare", lambda: clock.__setitem__(0, 2))
    attempts = []

    def resume():
        attempts.append(controller.remaining(3))
        clock[0] += attempts[-1]
        raise ValueError("not restored")

    monkeypatch.setattr(controller, "resume", resume)
    with pytest.raises(ValueError, match="not restored"):
        module.run_controller(controller, restore_only=True)
    assert attempts == [3, 3]
    assert clock[0] == controller.cleanup_deadline == 8
    assert not controller.cleanup()
    assert attempts == [3, 3]


def test_wakeup_changes_only_owned_branch_and_preserves_original_run(controller, monkeypatch, clock):
    controller.pr, controller.run_id, controller.canary_cleanup_needed = 4, 99, True
    api = Mock(
        side_effect=[
            {"object": {"sha": "head"}},
            {"tree": {"sha": "base-tree"}},
            {"sha": "wakeup-tree"},
            {"sha": "wakeup-commit"},
            {},
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    controller.wakeup("held", 2, 10)
    assert api.call_args_list[0] == call(f"git/ref/heads/{controller.branch}", None, "GET", timeout=10)
    tree = api.call_args_list[2].args[1]
    assert tree["base_tree"] == "base-tree"
    assert tree["tree"] == [
        {"path": ".osdc-drain-wakeup", "mode": "100644", "type": "blob", "content": "abc123:held:2\n"}
    ]
    assert api.call_args_list[3].args[1]["parents"] == ["head"]
    assert api.call_args_list[4] == call(
        f"git/refs/heads/{controller.branch}", {"sha": "wakeup-commit", "force": False}, "PATCH", timeout=10
    )
    assert controller.run_id == 99
    assert controller.pr == 4


@pytest.mark.parametrize("invalid", ["draining", "branch", "pr", "run"])
def test_wakeup_refuses_drain_or_missing_canary(controller, monkeypatch, invalid):
    controller.pr, controller.run_id, controller.canary_cleanup_needed = 4, 99, True
    if invalid == "draining":
        controller.draining = True
    elif invalid == "branch":
        controller.canary_cleanup_needed = False
    elif invalid == "pr":
        controller.pr = None
    else:
        controller.run_id = None
    api = Mock()
    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(RuntimeError, match="active canary outside drain"):
        controller.wakeup("held", 1, 10)
    api.assert_not_called()


def test_wakeup_api_calls_share_phase_deadline(controller, monkeypatch, clock):
    controller.pr, controller.run_id, controller.canary_cleanup_needed = 4, 99, True

    def api(*args, **kwargs):
        remaining = 7 - clock[0]
        assert kwargs["timeout"] == min(30, remaining)
        clock[0] += 5
        return {"object": {"sha": "head"}, "tree": {"sha": "base-tree"}}

    request = Mock(side_effect=api)
    monkeypatch.setattr(controller, "api", request)
    with pytest.raises(TimeoutError, match="wakeup deadline exceeded"):
        controller.wakeup("held", 1, 7)
    assert request.call_count == 2  # Deadline expires before any write.


@pytest.fixture
def waking_controller(controller, monkeypatch, clock):
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "capacities", Mock(return_value={"cpu": 3}))
    monkeypatch.setattr(controller, "wakeup", Mock())
    return controller


def test_wakeup_stops_after_assignment_and_waits_for_completion(waking_controller, clock):
    test = waking_controller

    def check():
        if clock[0] >= 35:
            test.assigned_phases.add("held")
        return clock[0] >= 60

    test.wait_with_wakeup("held succeeds", "held", check, 100)
    assert test.wakeup.call_args_list == [call("held", n, 100) for n in (1, 2, 3)]
    assert clock[0] == 60


def test_wakeup_attempts_are_bounded_then_waits_until_deadline(waking_controller, clock):
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        waking_controller.wait_with_wakeup("held succeeds", "held", lambda: False, 130)
    assert waking_controller.wakeup.call_args_list == [
        call("held", n, 130) for n in range(1, module.WAKEUP_ATTEMPTS + 1)
    ]
    assert waking_controller.capacities.call_args_list == [call({"cpu"})] * module.WAKEUP_ATTEMPTS
    assert clock[0] == 130


def test_early_wakeup_is_repeated_after_broker_capacity_catches_up(waking_controller, clock):
    test = waking_controller
    sent = []
    assigned = False

    def wakeup(phase, attempt, deadline):
        nonlocal assigned
        sent.append((clock[0], attempt))
        # The local metric is already 3, while the first broker poll still used 0.
        # Pulses before that poll completes are accepted but do not wake old jobs.
        assigned = clock[0] >= 50

    def check():
        if assigned:
            test.assigned_phases.add("held")
        return assigned

    test.wakeup.side_effect = wakeup
    test.wait_with_wakeup("held assigned", "held", check, 100)
    assert sent == [(0, 1), (15, 2), (30, 3), (45, 4), (60, 5)]
    assert clock[0] == 65


def test_each_wakeup_rechecks_capacity_without_consuming_an_attempt(waking_controller, monkeypatch, clock):
    test = waking_controller
    monkeypatch.setattr(test, "capacities", lambda expected: {"cpu": 0 if 15 <= clock[0] < 35 else 3})
    test.wait_with_wakeup("held assigned", "held", lambda: clock[0] >= 40, 100)
    assert test.wakeup.call_args_list == [call("held", 1, 100), call("held", 2, 100)]


def test_wakeup_retries_stay_inside_shared_main_deadline(waking_controller, clock):
    with waking_controller.deadline_scope(20), pytest.raises(TimeoutError, match="deadline exceeded"):
        waking_controller.wait_with_wakeup("held assigned", "held", lambda: False, 100)
    assert waking_controller.wakeup.call_args_list == [call("held", 1, 20), call("held", 2, 20)]
    assert clock[0] == 20


@pytest.mark.parametrize("applied", [False, True])
def test_wakeup_ref_timeout_reuses_commit_and_verifies_write(controller, monkeypatch, clock, applied):
    controller.canary_cleanup_needed = True
    controller.pr, controller.run_id = 42, 10
    head = "original"
    writes = []
    patch_count = 0

    def api(endpoint, payload=None, method="GET", **kwargs):
        nonlocal head, patch_count
        if endpoint == f"git/ref/heads/{controller.branch}":
            return {"object": {"sha": head}}
        if endpoint == "git/commits/original":
            return {"tree": {"sha": "base-tree"}}
        writes.append((endpoint, payload, method))
        if endpoint == "git/trees":
            return {"sha": "wakeup-tree"}
        if endpoint == "git/commits":
            return {"sha": "wakeup-commit"}
        assert endpoint == f"git/refs/heads/{controller.branch}"
        patch_count += 1
        if patch_count == 1:
            if applied:
                head = payload["sha"]
            raise subprocess.TimeoutExpired("gh", 1)
        head = payload["sha"]
        return {}

    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(subprocess.TimeoutExpired):
        controller.wakeup("held", 1, 100)
    controller.wakeup("held", 1, 100)
    assert head == "wakeup-commit"
    assert [e for e, _, _ in writes].count("git/trees") == 1
    assert [e for e, _, _ in writes].count("git/commits") == 1
    patches = [p for e, p, _ in writes if e.startswith("git/refs/")]
    assert patches == [{"sha": "wakeup-commit", "force": False}] * (1 if applied else 2)
    assert controller.run_id == 10


def test_unresolved_wakeup_refuses_foreign_branch_update(controller, monkeypatch, clock):
    controller.canary_cleanup_needed = True
    controller.pr, controller.run_id = 42, 10
    controller.wakeup_intents[("held", 1)] = {"parent": "old", "tree": "tree", "commit": "ours"}
    api = Mock(return_value={"object": {"sha": "foreign"}})
    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(RuntimeError, match="branch changed"):
        controller.wakeup("held", 1, 100)
    api.assert_called_once_with(f"git/ref/heads/{controller.branch}", None, "GET", timeout=30)


@pytest.mark.parametrize("failed_stage", ["git/trees", "git/commits"])
def test_wakeup_object_write_timeout_never_publishes_an_unconfirmed_commit(
    controller, monkeypatch, clock, failed_stage
):
    controller.canary_cleanup_needed = True
    controller.pr, controller.run_id = 42, 10
    calls = []
    failed = False

    def api(endpoint, payload=None, method="GET", **kwargs):
        nonlocal failed
        calls.append((endpoint, payload, method))
        if endpoint == f"git/ref/heads/{controller.branch}":
            return {"object": {"sha": "original"}}
        if endpoint == "git/commits/original":
            return {"tree": {"sha": "base-tree"}}
        if endpoint == failed_stage and not failed:
            failed = True
            raise subprocess.TimeoutExpired("gh", 1)
        if endpoint == "git/trees":
            return {"sha": "tree"}
        if endpoint == "git/commits":
            return {"sha": "confirmed-commit"}
        return {}

    monkeypatch.setattr(controller, "api", api)
    with pytest.raises(subprocess.TimeoutExpired):
        controller.wakeup("held", 1, 100)
    assert not any(method == "PATCH" for _, _, method in calls)
    controller.wakeup("held", 1, 100)
    assert [(e, p) for e, p, method in calls if method == "PATCH"] == [
        (f"git/refs/heads/{controller.branch}", {"sha": "confirmed-commit", "force": False})
    ]


def test_wakeup_waits_for_target_capacity_and_preserves_regional_zeros(waking_controller, monkeypatch, clock):
    test = waking_controller
    test.desired["excluded"] = 0
    monkeypatch.setattr(
        test,
        "scale_sets",
        lambda: [*ars(), {"metadata": {"name": "excluded"}, "spec": {"maxRunners": 0, "runnerScaleSetName": "gpu"}}],
    )
    capacities = Mock(side_effect=lambda expected=None: {"cpu": 0 if clock[0] < 15 else 3, "excluded": 0})
    monkeypatch.setattr(test, "capacities", capacities)
    test.wait_with_wakeup("inflight starts", "inflight", lambda: clock[0] >= 20, 100)
    # Capacity is probed every poll interval until it becomes positive at clock=15.
    assert capacities.call_args_list == [call({"cpu"})] * 4
    test.wakeup.assert_called_once_with("inflight", 1, 100)


@pytest.mark.parametrize("broken", ["limits", "metrics", "target-capacity"])
def test_wakeup_requires_restored_limits_and_capacity(waking_controller, monkeypatch, clock, broken):
    test = waking_controller
    if broken == "limits":
        monkeypatch.setattr(test, "scale_sets", lambda: ars(0))
    elif broken == "metrics":
        monkeypatch.setattr(test, "capacities", Mock(side_effect=ValueError("missing metric")))
    else:
        monkeypatch.setattr(test, "capacities", Mock(return_value={"cpu": 0, "another": 3}))
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        test.wait_with_wakeup("inflight starts", "inflight", lambda: False, 20)
    if broken == "limits":
        test.capacities.assert_not_called()
    else:
        # Capacity is probed every poll interval until the deadline.
        assert len(test.capacities.call_args_list) >= 2
        assert all(c == call({"cpu"}) for c in test.capacities.call_args_list)
    test.wakeup.assert_not_called()


def test_failed_wakeup_write_is_retried_until_deadline(waking_controller, clock):
    waking_controller.wakeup.side_effect = subprocess.TimeoutExpired("gh", 1)
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        waking_controller.wait_with_wakeup("inflight starts", "inflight", lambda: False, 20)
    # Transient failures should be retried on every poll interval until the deadline.
    assert waking_controller.wakeup.call_args_list == [call("inflight", 1, 20)] * 4
    assert clock[0] == 20


def test_workaround_never_wakes_during_drain(waking_controller):
    waking_controller.draining = True
    with pytest.raises(RuntimeError, match="forbidden during drain"):
        waking_controller.wait_with_wakeup("held succeeds", "held", lambda: False, 20)
    waking_controller.wakeup.assert_not_called()


def test_workaround_propagates_original_failure_without_waking(waking_controller):
    with pytest.raises(RuntimeError, match="wrong group"):
        waking_controller.wait_with_wakeup("held succeeds", "held", Mock(side_effect=RuntimeError("wrong group")), 20)
    waking_controller.wakeup.assert_not_called()


@pytest.mark.parametrize("invalid", [None, True, 0, -1])
def test_original_job_ids_must_be_valid(controller, monkeypatch, invalid):
    monkeypatch.setattr(controller, "jobs", lambda: [job(job_id=invalid)])
    with pytest.raises(RuntimeError, match="Invalid inflight probe job IDs"):
        controller.inflight_ready()


def test_original_jobs_cannot_be_replaced_by_successful_new_jobs(controller, monkeypatch):
    jobs = Mock(return_value=[job(status="queued")])
    monkeypatch.setattr(controller, "jobs", jobs)
    assert not controller.completed("inflight", 1)
    jobs.return_value = [job(job_id=2)]
    with pytest.raises(RuntimeError, match="Original inflight probe jobs changed"):
        controller.completed("inflight", 1)


def test_duplicate_held_job_ids_fail(controller, monkeypatch):
    monkeypatch.setattr(controller, "jobs", lambda: [job("held", status="queued")] * 3)
    with pytest.raises(RuntimeError, match="Invalid held probe job IDs"):
        controller.held_ready()


@pytest.mark.parametrize("finished", [True, False])
def test_cleanup_handles_wakeup_completion_race(controller, monkeypatch, finished):
    controller.canary_cleanup_needed = True
    monkeypatch.setattr(controller, "runs", lambda: [canary_run(99, "in_progress")])
    api = Mock(
        side_effect=[
            [],  # No open PRs.
            subprocess.CalledProcessError(1, "gh"),
            {"status": "completed" if finished else "in_progress"},
            {},
        ]
    )
    monkeypatch.setattr(controller, "api", api)
    assert controller.cleanup() is finished
    assert controller.canary_cleanup_needed is not finished
    if finished:
        assert api.call_args_list[-1] == call(f"git/refs/heads/{controller.branch}", method="DELETE")
    else:
        assert api.call_count == 3


def test_restore_only_and_cleanup_never_wake_probes(controller, monkeypatch, controller_runtime):
    controller.draining = True
    monkeypatch.setattr(controller, "prepare", Mock())
    monkeypatch.setattr(controller, "recipe", Mock())
    monkeypatch.setattr(controller, "scale_sets", lambda: ars())
    monkeypatch.setattr(controller, "kube", lambda *a: '{"items":[]}')
    wake = Mock(side_effect=AssertionError("restore must not create wakeup jobs"))
    monkeypatch.setattr(controller, "wakeup", wake)
    module.run_controller(controller, restore_only=True)
    assert not controller.draining
    wake.assert_not_called()


@pytest.fixture
def recovery_state(controller, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(module, "PROC_ROOT", proc)
    controller.recovery_file = tmp_path / "recovery.json"
    owner = controller.record_recovery()
    return proc, owner[module.DRAIN_OWNER_ENV]


def test_recovery_intent_precedes_writer_and_does_not_tag_controller(controller, recovery_state):
    _, token = recovery_state
    assert json.loads(controller.recovery_file.read_text()) == {"cluster": CLUSTER, "token": token}
    assert module.DRAIN_OWNER_ENV not in controller.env_overrides
    with pytest.raises(FileExistsError):
        controller.record_recovery()
    assert json.loads(controller.recovery_file.read_text())["token"] == token


def test_runner_local_recovery_requires_proc_before_mutation(controller, tmp_path, monkeypatch):
    controller.recovery_file = tmp_path / "intent"
    monkeypatch.setattr(module, "PROC_ROOT", tmp_path / "missing-proc")
    with pytest.raises(RuntimeError, match="Linux /proc"):
        controller.record_recovery()
    assert not controller.recovery_file.exists()
    with pytest.raises(RuntimeError, match="Linux /proc"):
        controller.stop_recorded_drain()


def write_proc(root, pid, group, token, state="S"):
    process = root / str(pid)
    process.mkdir()
    (process / "stat").write_text(f"{pid} (name with ) spaces) {state} 1 {group} 0")
    (process / "environ").write_bytes(f"PATH=/bin\0{module.DRAIN_OWNER_ENV}={token}\0".encode())
    return process


def test_recovery_finds_orphan_descendants_not_unrelated_reused_pids(controller, recovery_state):
    proc, token = recovery_state
    write_proc(proc, 123, 123, "unrelated")
    write_proc(proc, 456, 123, token)  # leader exited; descendant retains ownership.
    write_proc(proc, 789, 789, token, state="Z")
    (proc / "999").mkdir()  # disappeared before inspection
    assert controller.recorded_drain_groups(token) == {123}
    (proc / "456" / "environ").write_bytes(b"PATH=/bin\0")
    assert controller.recorded_drain_groups(token) == set()


@pytest.mark.parametrize("mode", ["cleanup", "restore-only"])
def test_recovery_skips_unreadable_environ_and_stops_owned_writer(
    controller, recovery_state, monkeypatch, clock, controller_runtime, mode
):
    proc, token = recovery_state
    unrelated = write_proc(proc, 123, 123, "unrelated")
    writer = write_proc(proc, 456, 456, token)
    read_bytes = module.Path.read_bytes

    def read_environment(path):
        if path == unrelated / "environ":
            raise PermissionError("non-dumpable process")
        return read_bytes(path)

    monkeypatch.setattr(module.Path, "read_bytes", read_environment)
    assert controller.recorded_drain_groups(token) == {456}

    def stop_writer(group, sig):
        assert (group, sig) == (456, signal.SIGTERM)
        (writer / "environ").write_bytes(b"")

    kill = Mock(side_effect=stop_writer)
    monkeypatch.setattr(module.os, "killpg", kill)
    monkeypatch.setattr(controller, "prepare", Mock())

    def resume():
        assert not controller.recorded_drain_groups(token)
        controller.draining = False

    resume = Mock(side_effect=resume)
    monkeypatch.setattr(controller, "resume", resume)
    if mode == "cleanup":
        controller.draining = True
        assert controller.cleanup()
    else:
        module.run_controller(controller, restore_only=True)
    kill.assert_called_once_with(456, signal.SIGTERM)
    resume.assert_called_once_with()
    assert not controller.draining


@pytest.mark.parametrize("mode", ["cleanup", "restore-only"])
@pytest.mark.parametrize("operation", ["directory-stat", "owned-stat"])
def test_recovery_does_not_ignore_permission_errors_outside_environ(
    controller, recovery_state, monkeypatch, controller_runtime, mode, operation
):
    proc, token = recovery_state
    writer = write_proc(proc, 456, 456, token)
    method = "stat" if operation == "directory-stat" else "read_text"
    target = writer if operation == "directory-stat" else writer / "stat"
    original = getattr(module.Path, method)

    def denied_read(path, *args, **kwargs):
        if path == target:
            raise PermissionError("writer inspection denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module.Path, method, denied_read)
    with pytest.raises(PermissionError, match="writer inspection denied"):
        controller.recorded_drain_groups(token)

    kill, prepare, resume = Mock(), Mock(), Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    monkeypatch.setattr(controller, "prepare", prepare)
    monkeypatch.setattr(controller, "resume", resume)
    if mode == "cleanup":
        controller.draining = True
        assert not controller.cleanup()
        assert controller.draining
    else:
        with pytest.raises(PermissionError, match="writer inspection denied"):
            module.run_controller(controller, restore_only=True)
    kill.assert_not_called()
    prepare.assert_not_called()
    resume.assert_not_called()
    assert controller.recovery_file.exists()


@pytest.mark.parametrize(
    "stat",
    ["", "123 (truncated", "123 (drain)", "123 (drain) S", "123 (drain) S 1", "123 (drain) S 1 invalid"],
)
def test_recovery_rejects_malformed_owned_stat_before_signalling_or_resume(
    controller, recovery_state, monkeypatch, stat
):
    proc, token = recovery_state
    write_proc(proc, 456, 456, token)
    process = write_proc(proc, 123, 123, token)
    (process / "stat").write_text(stat)
    with pytest.raises(ValueError, match="drain writer PID 123"):
        controller.recorded_drain_groups(token)
    kill, resume = Mock(), Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    monkeypatch.setattr(controller, "resume", resume)
    controller.draining = True
    assert not controller.cleanup()
    kill.assert_not_called()
    resume.assert_not_called()
    assert controller.draining
    assert controller.recovery_file.exists()


def test_recovery_ignores_malformed_stat_for_unrelated_process(controller, recovery_state):
    proc, token = recovery_state
    process = write_proc(proc, 123, 123, "unrelated")
    (process / "stat").write_text("")
    write_proc(proc, 456, 456, token)
    assert controller.recorded_drain_groups(token) == {456}


def test_recovery_does_not_inspect_other_users(controller, recovery_state, monkeypatch):
    proc, token = recovery_state
    write_proc(proc, 123, 123, token)
    monkeypatch.setattr(module.os, "getuid", lambda: -1)
    assert controller.recorded_drain_groups(token) == set()


@pytest.mark.parametrize("group", [1, 0, "own"])
def test_recovery_refuses_unsafe_process_group(controller, recovery_state, group):
    proc, token = recovery_state
    write_proc(proc, 123, os.getpgrp() if group == "own" else group, token)
    with pytest.raises(RuntimeError, match="unsafe process group"):
        controller.recorded_drain_groups(token)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"cluster": "production", "token": "a" * 32},
        {"cluster": CLUSTER, "token": "z" * 32},
        {"cluster": CLUSTER, "token": None},
    ],
)
def test_recovery_rejects_invalid_intent_without_signalling(controller, recovery_state, monkeypatch, state):
    controller.recovery_file.write_text(json.dumps(state))
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(ValueError, match="Invalid runner-local"):
        controller.stop_recorded_drain()
    kill.assert_not_called()


@pytest.mark.parametrize("content", ['{"cluster": "', "null", "[]", "42", '"x"', "true"])
def test_recovery_treats_corrupt_intent_as_unfenceable_without_signalling(
    controller, recovery_state, monkeypatch, caplog, content
):
    controller.recovery_file.write_text(content)
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    controller.stop_recorded_drain()
    kill.assert_not_called()
    assert "unreadable or corrupt" in caplog.text


@pytest.mark.parametrize("content", ['{"cluster": "', "null", "[]", "42", '"x"', "true"])
def test_restore_only_attempts_resume_despite_corrupt_recovery_file(controller, recovery_state, monkeypatch, content):
    controller.recovery_file.write_text(content)
    monkeypatch.setattr(controller, "prepare", Mock())

    def resume():
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    module.run_controller(controller, restore_only=True)
    assert not controller.draining


@pytest.mark.parametrize("survives_term", [False, True])
def test_recovery_stops_owned_writers_before_restore(
    controller, recovery_state, monkeypatch, clock, controller_runtime, survives_term
):
    proc, token = recovery_state
    process = write_proc(proc, 456, 123, token)
    signals = []

    def kill(group, sig):
        assert group == 123
        signals.append(sig)
        if sig == signal.SIGKILL or not survives_term:
            (process / "environ").write_bytes(b"")

    monkeypatch.setattr(module.os, "killpg", kill)
    monkeypatch.setattr(controller, "prepare", Mock())

    def resume():
        assert not controller.recorded_drain_groups(token)
        controller.draining = False

    monkeypatch.setattr(controller, "resume", resume)
    module.run_controller(controller, restore_only=True)
    assert signals == ([signal.SIGTERM, signal.SIGKILL] if survives_term else [signal.SIGTERM])
    assert controller.recovery_attempts == 1


def test_recovery_refuses_to_resume_when_writer_cannot_be_stopped(
    controller, recovery_state, monkeypatch, clock, controller_runtime
):
    proc, token = recovery_state
    write_proc(proc, 456, 123, token)
    monkeypatch.setattr(module.os, "killpg", Mock())
    resume = Mock()
    monkeypatch.setattr(controller, "resume", resume)
    with pytest.raises(TimeoutError, match="recorded drain writers stopped"):
        module.run_controller(controller, restore_only=True)
    resume.assert_not_called()
    assert clock[0] < controller.args.restore_timeout


def test_recovery_process_disappears_before_signal(controller, recovery_state, monkeypatch):
    groups = Mock(side_effect=[{123}, set()])
    monkeypatch.setattr(controller, "recorded_drain_groups", groups)
    monkeypatch.setattr(module.os, "killpg", Mock(side_effect=ProcessLookupError))
    controller.stop_recorded_drain()


def test_recovery_file_cli_environment(monkeypatch, tmp_path):
    state = tmp_path / "recovery.json"
    monkeypatch.setenv("OSDC_DRAIN_RECOVERY_FILE", str(state))
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER, "--restore-only"])
    assert module.parse_args().recovery_file == state


def test_ci_timeouts_bound_controller_and_recovery(monkeypatch):
    root = module.UPSTREAM.parent
    job = yaml.safe_load((root / ".github/workflows/_osdc-slow-tests.yml").read_text())["jobs"]["drain-test"]
    test = next(step for step in job["steps"] if step.get("id") == "drain")
    restore = next(step for step in job["steps"] if step.get("id") == "restore")
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER])
    defaults = module.parse_args()
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", CLUSTER, *shlex.split(test["run"])[3:]])
    ci = module.parse_args()
    for name in ("run_timeout", "cleanup_timeout", "start_timeout", "finish_timeout", "drain_timeout"):
        assert getattr(ci, name) == getattr(defaults, name)
    # The run budget is a shared cap, not a promise that every phase can use its
    # full allowance. Check queue aging headroom and the actual drain phase cap
    # separately from the step's cleanup/recovery allowance.
    assert ci.start_timeout > 15 * 60
    assert ci.finish_timeout > 15 * 60
    assert ci.run_timeout > ci.start_timeout + ci.finish_timeout
    assert ci.drain_timeout < module.DRAIN_PHASE_TIMEOUT < ci.run_timeout
    assert test["timeout-minutes"] * 60 >= ci.run_timeout + ci.cleanup_timeout + 300
    assert job["timeout-minutes"] > test["timeout-minutes"] + restore["timeout-minutes"]


def test_workflow_cancellation_stops_test_and_attempts_recovery_under_lock():
    root = module.UPSTREAM.parent
    jobs = yaml.safe_load((root / ".github/workflows/_osdc-slow-tests.yml").read_text())["jobs"]
    drain = jobs["drain-test"]
    assert drain["if"] == "${{ inputs.cluster == 'meta-staging-aws-uw1' && !cancelled() && !failure() }}"
    assert "always()" not in drain["if"]
    assert "!cancelled()" in drain["if"]
    assert "!failure()" in drain["if"]
    assert set(drain["needs"]) == {"load-test", "compactor", "janitor"}
    assert drain["concurrency"] == {"group": "osdc-tofu-${{ inputs.cluster }}", "cancel-in-progress": False}
    steps = drain["steps"]
    for step in steps[:4]:
        assert "success() && !cancelled()" in step["if"]
    restore = next(step for step in steps if step.get("id") == "restore")
    assert "always()" in restore["if"]
    assert "cancelled()" not in restore["if"]
    assert restore["timeout-minutes"] == 11
    assert "--restore-timeout 600" in restore["run"]
    assert '[[ -f "$RECOVERY_FILE" ]]' in restore["run"]
    test = next(step for step in steps if step.get("id") == "drain")
    assert "--defer-recovery" in test["run"]
    assert "--defer-recovery" not in restore["run"]
    assert "--recovery-file" in test["run"]
    assert "--recovery-file" in restore["run"]
    workflow = yaml.safe_load((root / ".github/workflows/osdc-pre-merge.yml").read_text())
    assert workflow["concurrency"] == {"group": "osdc-staging", "cancel-in-progress": False}
    caller = workflow["jobs"]["slow-tests"]
    assert caller["uses"] == "./.github/workflows/_osdc-slow-tests.yml"
    assert caller["with"] == {"cluster": CLUSTER, "environment": "osdc-staging"}
    assert test["env"]["OSDC_DRAIN_TEST_LIVE"] is True
    assert restore["env"]["OSDC_DRAIN_TEST_LIVE"] is True
    assert "always()" not in caller["if"]
    assert "needs.deploy-fast.result == 'success'" in caller["if"]


def test_cleanup_fences_writer_when_launch_interrupted_before_handle_saved(controller, recovery_state, monkeypatch):
    controller.draining = True
    controller.drain = None
    events = []
    monkeypatch.setattr(controller, "stop_recorded_drain", lambda: events.append("fenced"))

    def resume():
        assert events == ["fenced"]
        controller.draining = False
        events.append("restored")

    monkeypatch.setattr(controller, "resume", resume)
    assert controller.cleanup()
    assert events == ["fenced", "restored"]


@pytest.mark.parametrize("failure", [RuntimeError("test failed"), SystemExit(130), TimeoutError("test timed out")])
def test_ci_failure_stops_writers_and_defers_recovery_without_canary_api(
    controller, recovery_state, monkeypatch, controller_runtime, failure
):
    controller.defer_recovery = True
    controller.draining = controller.canary_cleanup_needed = True
    events = []

    def run():
        raise failure

    monkeypatch.setattr(controller, "run", run)
    monkeypatch.setattr(controller, "stop_drain", lambda: events.append("stop"))
    monkeypatch.setattr(controller, "stop_recorded_drain", lambda: events.append("fence"))
    resume, api, runs = Mock(), Mock(), Mock()
    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "api", api)
    monkeypatch.setattr(controller, "runs", runs)
    with pytest.raises(type(failure)) as exc:
        module.run_controller(controller)
    assert exc.value is failure
    assert events == ["stop", "fence"]
    resume.assert_not_called()
    api.assert_not_called()
    runs.assert_not_called()
    assert controller.draining
    assert controller.canary_cleanup_needed
    assert controller.recovery_file.exists()
    module.atexit.unregister.assert_called_once_with(controller.cleanup)


def test_ci_success_cleans_canary_and_removes_recovery_file(
    controller, recovery_state, monkeypatch, controller_runtime
):
    controller.defer_recovery = True
    controller.canary_cleanup_needed = True
    monkeypatch.setattr(controller, "run", Mock())  # successful run already resumed the cluster
    resume = Mock()
    monkeypatch.setattr(controller, "resume", resume)
    monkeypatch.setattr(controller, "runs", lambda: [canary_run(1, "queued")])

    def api_side_effect(*a, **kw):
        if a[0].startswith("pulls?"):
            return [canary_pr(3)]
        return {}

    api = Mock(side_effect=api_side_effect)
    monkeypatch.setattr(module, "api", api)
    module.run_controller(controller)
    resume.assert_not_called()
    assert not controller.canary_cleanup_needed
    assert not controller.skip_canary_cleanup
    assert api.call_args_list == [
        call(f"pulls?head=pytorch:{controller.branch}&state=open&per_page=100", None, "GET", deadline=ANY),
        call("actions/runs/1/cancel", None, "POST", deadline=ANY),
        call("pulls/3", {"state": "closed"}, "PATCH", deadline=ANY),
        call(f"git/refs/heads/{controller.branch}", None, "DELETE", deadline=ANY),
    ]
    assert not controller.recovery_file.exists()


def test_ci_success_keeps_recovery_file_when_removal_fails(controller, recovery_state, monkeypatch, controller_runtime):
    controller.defer_recovery = True
    monkeypatch.setattr(controller, "run", Mock())
    monkeypatch.setattr(type(controller.recovery_file), "unlink", Mock(side_effect=OSError("read-only filesystem")))

    with pytest.raises(RuntimeError, match="Test passed but cleanup failed"):
        module.run_controller(controller)

    assert controller.recovery_file.exists()


def test_ci_deferral_refuses_to_race_surviving_writers(controller, recovery_state, monkeypatch, clock):
    controller.defer_recovery = True
    controller.draining = controller.canary_cleanup_needed = True
    monkeypatch.setattr(controller, "stop_recorded_drain", Mock(side_effect=OSError("writer survives")))
    assert not controller.cleanup()
    assert controller.draining
    assert controller.canary_cleanup_needed


def test_ci_defer_recovery_cli_requires_handoff_and_preserves_local_default(monkeypatch, tmp_path):
    state = tmp_path / "intent.json"
    monkeypatch.setattr(
        sys, "argv", ["test", "--cluster-id", CLUSTER, "--defer-recovery", "--recovery-file", str(state)]
    )
    args = module.parse_args()
    assert args.defer_recovery
    assert args.recovery_file == state
    assert not args.restore_only


@pytest.mark.parametrize("cluster", [CLUSTER, "meta-staging-aws-ue1"])
def test_cluster_config_drives_preparation_probes_and_recovery(controller, monkeypatch, cluster):
    group, prefix = "staging-canary-runners", "staging-"
    controller.args.cluster_id = cluster
    (controller.args.root_dir / "clusters.yaml").write_text(
        yaml.safe_dump(
            {
                "clusters": {
                    cluster: {
                        "arc-runners": {
                            "runner_group": group,
                            "runner_name_prefix": prefix,
                            "github_config_url": ORG_URL,
                        }
                    }
                }
            }
        )
    )
    target = module.DrainTest(controller.args, "abc123")
    command = Mock(return_value=subprocess.CompletedProcess([], 0, '{"cpu":8}', ""))
    monkeypatch.setattr(module, "command", command)
    target.prepare()
    assert target.cluster == cluster
    assert target.group == group
    assert target.branch == f"osdc-drain-test-{cluster}"
    assert command.call_args_list[0].args[0] == ["just", "kubeconfig", cluster]
    assert command.call_args_list[1].args[0][-1] == cluster
    for probe in yaml.safe_load(target.workflow())["jobs"].values():
        assert probe["runs-on"] == {"group": group, "labels": [f"{prefix}l-x86iamx-8-16"]}
    items = ars()
    items[0]["spec"]["runnerScaleSetName"] = target.probe_label
    assert target.check_probe(items) == "cpu"
    with pytest.raises(ValueError, match=cluster):
        target.check_probe([])
    target.recipe("resume-runners")
    assert command.call_args.args[0] == ["just", "resume-runners", cluster]
    target.recovery_file = controller.args.root_dir / "target-recovery.json"
    monkeypatch.setattr(module, "PROC_ROOT", controller.args.root_dir)
    target.record_recovery()
    assert json.loads(target.recovery_file.read_text())["cluster"] == cluster
    other_args = argparse.Namespace(**vars(controller.args))
    other_args.cluster_id = "another-cluster"
    other_args.recovery_file = target.recovery_file
    other = module.DrainTest(other_args, "def456")
    kill = Mock()
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(ValueError, match="Invalid runner-local"):
        other.stop_recorded_drain()
    kill.assert_not_called()


@pytest.mark.parametrize("cluster", [CLUSTER, "meta-staging-aws-ue1"])
def test_cli_and_dry_run_use_existing_cluster_config(monkeypatch, capsys, cluster):
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", cluster, "--dry-run"])
    cfg = module.load_cluster_config(module.UPSTREAM / "clusters.yaml", cluster)
    monkeypatch.setattr(module, "command", Mock(side_effect=AssertionError("external command")))
    module.main()
    for probe in yaml.safe_load(capsys.readouterr().out)["jobs"].values():
        assert probe["runs-on"] == {
            "group": module.configured_runner_group(cfg),
            "labels": [module.configured_probe_label(cfg, cluster)],
        }


@pytest.mark.parametrize("mode", [[], ["--dry-run"], ["--restore-only"], ["--defer-recovery"]])
def test_cli_rejects_unknown_clusters_before_side_effects(monkeypatch, tmp_path, ci_environment, caplog, mode):
    cluster = "unknown-cluster"
    state = tmp_path / "intent.json"
    monkeypatch.setattr(sys, "argv", ["test", "--cluster-id", cluster, "--recovery-file", str(state), *mode])
    operations = [Mock() for _ in range(6)]
    command, controller, run, signals, register, kill = operations
    monkeypatch.setattr(module, "command", command)
    monkeypatch.setattr(module, "DrainTest", controller)
    monkeypatch.setattr(module, "run_controller", run)
    monkeypatch.setattr(module.signal, "signal", signals)
    monkeypatch.setattr(module.atexit, "register", register)
    monkeypatch.setattr(module.os, "killpg", kill)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert f"Cluster '{cluster}' not found" in caplog.text
    for operation in operations:
        operation.assert_not_called()
    assert not state.exists()


@pytest.mark.parametrize("output", ["", "not-json", "null", "true", "7", '"text"'])
@pytest.mark.parametrize("method", ["GET", "PATCH", "POST"])
def test_api_rejects_invalid_resource_responses(monkeypatch, output, method):
    monkeypatch.setattr(module, "command", Mock(return_value=Mock(stdout=output)))
    with pytest.raises(ValueError, match="Expected a JSON object or array"):
        module.api("pulls/42", method=method)


@pytest.mark.parametrize(("endpoint", "method"), [("git/refs/heads/test", "DELETE"), ("actions/runs/1/cancel", "POST")])
def test_api_accepts_empty_response_only_for_bodyless_operations(monkeypatch, endpoint, method):
    monkeypatch.setattr(module, "command", Mock(return_value=Mock(stdout=" \n")))
    assert module.api(endpoint, method=method) == {}
    monkeypatch.setattr(module, "command", Mock(return_value=Mock(stdout="truncated")))
    with pytest.raises(ValueError, match="Expected a JSON object or array"):
        module.api(endpoint, method=method)


@pytest.mark.parametrize("response", ['{"jobs": []}', '[{"id": 1}]'])
def test_api_read_retries_malformed_json_then_returns_resource(controller, monkeypatch, clock, response):
    command = Mock(side_effect=[Mock(stdout="truncated"), Mock(stdout=response)])
    monkeypatch.setattr(module, "command", command)
    results = []

    def read():
        results.append(controller.api("actions/runs/42/jobs"))
        return True

    controller.wait_for("GitHub resource", read, 10, poll=1)
    assert results == [json.loads(response)]
    assert clock[0] == 1


@pytest.mark.parametrize(
    "output", ["", "broken", "[]", "null", "{}", '{"cpu": true}', '{"cpu": -1}', '{"cpu": 1.5}', '{"": 8}']
)
def test_prepare_rejects_invalid_desired_limits(controller, monkeypatch, output):
    monkeypatch.setattr(controller, "recipe", Mock())
    monkeypatch.setattr(module, "command", Mock(return_value=Mock(stdout=output)))
    with pytest.raises(ValueError, match="nonempty runner_max_map"):
        controller.prepare()
    assert controller.desired == {"cpu": 8}


@pytest.mark.parametrize("permanent", [False, True])
def test_kubeconfig_preflight_retries_within_total_deadline(controller, monkeypatch, clock, permanent):
    recipe = Mock(
        side_effect=subprocess.SubprocessError("temporary kubeconfig failure")
        if permanent
        else [subprocess.SubprocessError("temporary kubeconfig failure"), None]
    )
    monkeypatch.setattr(controller, "recipe", recipe)
    command = Mock(return_value=Mock(stdout='{"cpu": 8}'))
    monkeypatch.setattr(module, "command", command)
    with controller.deadline_scope(10):
        if permanent:
            with pytest.raises(TimeoutError, match="kubeconfig available"):
                controller.prepare()
            command.assert_not_called()
        else:
            controller.prepare()
    assert recipe.call_count == 2
    assert clock[0] == (10 if permanent else 5)


@pytest.mark.parametrize("permanent", [False, True])
def test_initial_scale_set_read_retries_before_mutations(release_controller, monkeypatch, clock, permanent):
    test = release_controller
    monkeypatch.setattr(test, "api", Mock(return_value={}))
    sets = Mock(
        side_effect=subprocess.SubprocessError("API unavailable")
        if permanent
        else [subprocess.SubprocessError("API unavailable"), ars()]
    )
    monkeypatch.setattr(test, "scale_sets", sets)
    resume = test.resume
    with test.deadline_scope(20):
        if permanent:
            with pytest.raises(TimeoutError, match="initial runner scale sets available"):
                test.run()
            test.create_pr.assert_not_called()
            module.subprocess.Popen.assert_not_called()
            resume.assert_not_called()
        else:
            test.run()
            test.create_pr.assert_called_once()
    assert sets.call_count == 2
    assert clock[0] == (20 if permanent else 10)


@pytest.mark.parametrize("missing_group", [None, ""])
def test_held_terminal_group_failure_escapes_retry_loop(placement_controller, clock, missing_group):
    test, jobs, _pods, _nodes = placement_controller
    assert not test.held_completed()
    for item in jobs:
        item.update(status="completed", runner_group_name=missing_group)
    with pytest.raises(RuntimeError, match="Jobs did not succeed"):
        test.wait_for("held completion", test.held_completed, 1800)
    assert clock[0] == 0


@pytest.mark.parametrize("recorded", [False, True])
def test_held_job_ids_are_checked_before_any_placement_or_release(placement_controller, recorded):
    test, jobs, _pods, _nodes = placement_controller
    if recorded:
        for item in jobs:
            item["id"] += 1000
    else:
        test.probe_ids.clear()
    with pytest.raises(RuntimeError, match="Original held probe jobs changed"):
        test.held_completed()
    test.kube.assert_not_called()
    test.api.assert_not_called()
    assert not test.placements


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("moment", ["run", "cleanup-handoff"])
def test_cancel_signal_before_cleanup_never_allows_canary_writes(
    controller, monkeypatch, controller_runtime, signum, moment
):
    controller.defer_recovery = True
    controller.canary_cleanup_needed = True
    handlers = {}

    def install(sig, handler):
        previous = handlers.get(sig, signal.SIG_DFL)
        if moment == "cleanup-handoff" and handler == signal.SIG_IGN:
            handlers[signum](signum, None)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(module.signal, "signal", install)

    def run():
        if moment == "run":
            handlers[signum](signum, None)

    monkeypatch.setattr(controller, "run", run)
    api = Mock()
    monkeypatch.setattr(controller, "api", api)
    monkeypatch.setattr(controller, "runs", Mock())
    with pytest.raises(SystemExit) as exc:
        module.run_controller(controller)
    assert exc.value.code == 128 + signum
    assert controller.skip_canary_cleanup
    # Simulate the registered atexit fallback when a signal interrupted finally.
    assert controller.cleanup()
    api.assert_not_called()
    controller.runs.assert_not_called()


def test_successful_cluster_cleanup_does_not_print_manual_recovery(controller, monkeypatch, caplog):
    controller.draining = True
    monkeypatch.setattr(controller, "resume", lambda: setattr(controller, "draining", False))
    assert controller.cleanup()
    assert "Recovery command" not in caplog.text
    assert "RECOVERY REQUIRED" not in caplog.text


def test_staging_probe_runner_is_enabled_in_checked_in_configuration():
    config = yaml.safe_load((module.UPSTREAM / "clusters.yaml").read_text())
    cfg = module.load_cluster_config(module.UPSTREAM / "clusters.yaml", CLUSTER)
    label = module.configured_probe_label(cfg, CLUSTER)
    # Use the same offline def parsing and ARS-name normalization as resume-runners.
    desired = build_max_runners_map(module.UPSTREAM, config, CLUSTER)
    assert desired[compute_ars_name("", label)] > 0


def test_unknown_workflow_placeholder_fails_before_publication(controller, monkeypatch):
    read_text = module.Path.read_text

    def template(path, *args, **kwargs):
        result = read_text(path, *args, **kwargs)
        return result + "\n{{NEW_PLACEHOLDER}}\n" if path.name == "drain-test.yaml.tpl" else result

    monkeypatch.setattr(module.Path, "read_text", template)
    with pytest.raises(ValueError, match="Unresolved drain-test workflow placeholder"):
        controller.workflow()


def test_workflow_routes_cluster_and_recovery_path_through_environment():
    job = yaml.safe_load((module.UPSTREAM.parent / ".github/workflows/_osdc-slow-tests.yml").read_text())["jobs"][
        "drain-test"
    ]
    for step in job["steps"]:
        if step.get("id") in {"drain", "restore"}:
            assert "${{" not in step["run"]
            assert 'just drain-test "$CLUSTER"' in step["run"]
            assert '--recovery-file "$RECOVERY_FILE"' in step["run"]
            assert step["env"]["CLUSTER"] == "${{ inputs.cluster }}"
            assert (
                step["env"]["RECOVERY_FILE"]
                == "${{ runner.temp }}/osdc-drain-${{ github.run_id }}-${{ github.run_attempt }}.json"
            )
