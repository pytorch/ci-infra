"""Unit tests for dead-pod detection + cleanup in the reservation_expiry lambda.

Covers the gap where a pod evicted in place (node memory/disk pressure) leaves its
reservation stuck 'active' with a dead box until expiry. A container-level OOMKill
is recoverable (kubelet restarts it) and must NOT be cleaned up; a pod-level
eviction/failure must be finalized (snapshot + cleanup + free disk).
"""
import importlib.util
import pathlib
import types
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

_EXPIRY = (
    pathlib.Path(__file__).resolve().parents[3]
    / "terraform-gpu-devservers" / "lambda" / "reservation_expiry" / "index.py"
)


@pytest.fixture
def expiry():
    """Load the reservation_expiry lambda under a distinct module name (the bare
    name `index` is the reservation_processor)."""
    spec = importlib.util.spec_from_file_location("expiry_index", _EXPIRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pod(phase=None, reason=None, message=None, conditions=None, container_statuses=None):
    """Build a minimal duck-typed V1Pod-ish object for check_pod_dead."""
    return types.SimpleNamespace(
        status=types.SimpleNamespace(
            phase=phase,
            reason=reason,
            message=message,
            conditions=conditions,
            container_statuses=container_statuses,
        )
    )


def _cond(type_, status, reason=None, message=None):
    return types.SimpleNamespace(type=type_, status=status, reason=reason, message=message)


def _cstatus(name, running):
    state = types.SimpleNamespace(running=object() if running else None)
    return types.SimpleNamespace(name=name, state=state)


def _patch_pod(expiry, monkeypatch, pod=None, exc=None):
    """Make check_pod_dead's k8s read return `pod` (or raise `exc`)."""
    api = MagicMock()
    if exc is not None:
        api.read_namespaced_pod.side_effect = exc
    else:
        api.read_namespaced_pod.return_value = pod
    monkeypatch.setattr(expiry, "get_k8s_client", lambda: MagicMock())
    monkeypatch.setattr(expiry.client, "CoreV1Api", lambda _c: api)


class TestCheckPodDead:
    def test_evicted_pod_is_dead_with_reason(self, expiry, monkeypatch):
        pod = _pod(
            phase="Failed",
            reason="Evicted",
            message="The node was low on resource: memory. Container gpu-dev was using 55Gi.",
        )
        _patch_pod(expiry, monkeypatch, pod)
        reason = expiry.check_pod_dead("gpu-dev-abc")
        assert reason is not None
        assert "Evicted" in reason
        assert "memory" in reason

    def test_succeeded_pod_is_dead(self, expiry, monkeypatch):
        _patch_pod(expiry, monkeypatch, _pod(phase="Succeeded", reason=None))
        assert expiry.check_pod_dead("gpu-dev-abc") is not None

    def test_running_healthy_pod_not_dead(self, expiry, monkeypatch):
        pod = _pod(phase="Running", container_statuses=[_cstatus("gpu-dev", running=True)])
        _patch_pod(expiry, monkeypatch, pod)
        assert expiry.check_pod_dead("gpu-dev-abc") is None

    def test_pending_pod_not_dead(self, expiry, monkeypatch):
        _patch_pod(expiry, monkeypatch, _pod(phase="Pending"))
        assert expiry.check_pod_dead("gpu-dev-abc") is None

    def test_restarting_oom_container_not_dead(self, expiry, monkeypatch):
        # Container OOMKilled then restarted by kubelet -> running again -> recoverable.
        pod = _pod(phase="Running", container_statuses=[_cstatus("gpu-dev", running=True)])
        _patch_pod(expiry, monkeypatch, pod)
        assert expiry.check_pod_dead("gpu-dev-abc") is None

    def test_disruption_target_with_dead_main_container_is_dead(self, expiry, monkeypatch):
        pod = _pod(
            phase="Running",
            conditions=[_cond("DisruptionTarget", "True", reason="EvictionByEvictionAPI")],
            container_statuses=[_cstatus("gpu-dev", running=False)],
        )
        _patch_pod(expiry, monkeypatch, pod)
        reason = expiry.check_pod_dead("gpu-dev-abc")
        assert reason is not None and "EvictionByEvictionAPI" in reason

    def test_disruption_target_but_main_still_running_not_dead(self, expiry, monkeypatch):
        pod = _pod(
            phase="Running",
            conditions=[_cond("DisruptionTarget", "True", reason="TerminationByKubelet")],
            container_statuses=[_cstatus("gpu-dev", running=True)],
        )
        _patch_pod(expiry, monkeypatch, pod)
        assert expiry.check_pod_dead("gpu-dev-abc") is None

    def test_missing_pod_returns_none(self, expiry, monkeypatch):
        # 404 is handled by the separate missing-pod path, not here.
        _patch_pod(expiry, monkeypatch, exc=ApiException(status=404))
        assert expiry.check_pod_dead("gpu-dev-abc") is None

    def test_api_error_returns_none(self, expiry, monkeypatch):
        _patch_pod(expiry, monkeypatch, exc=ApiException(status=500))
        assert expiry.check_pod_dead("gpu-dev-abc") is None


class TestFinalizeDeadPod:
    def test_marks_failed_snapshots_and_frees_disk(self, expiry, monkeypatch):
        table = MagicMock()
        ddb = MagicMock()
        ddb.Table.return_value = table
        monkeypatch.setattr(expiry, "dynamodb", ddb)

        cleanup = MagicMock()
        monkeypatch.setattr(expiry, "cleanup_pod", cleanup)
        freed = MagicMock()
        monkeypatch.setattr(expiry, "mark_disk_not_in_use", freed)

        reservation = {
            "reservation_id": "9b1466cc-f272-40a6-90da-2bf0f4c1e599",
            "user_id": "ezyang@meta.com",
            "disk_name": "default",
            "pod_name": "gpu-dev-9b1466cc",
            "namespace": "gpu-dev",
            "ebs_volume_id": "vol-123",
        }
        reason = "Pod failed (Evicted): The node was low on resource: memory."
        expiry.expire_reservation_due_to_dead_pod(reservation, reason)

        # status -> failed with the reason, BEFORE cleanup (leaves the active set first)
        kwargs = table.update_item.call_args.kwargs
        vals = kwargs["ExpressionAttributeValues"]
        assert vals[":status"] == "failed"
        assert vals[":reason"] == reason

        # snapshot + delete the pod, then free the disk lock
        cleanup.assert_called_once()
        assert cleanup.call_args.args[0] == "gpu-dev-9b1466cc"
        freed.assert_called_once_with("ezyang@meta.com", "default")

    def test_disk_lock_freed_even_if_cleanup_fails(self, expiry, monkeypatch):
        table = MagicMock()
        ddb = MagicMock()
        ddb.Table.return_value = table
        monkeypatch.setattr(expiry, "dynamodb", ddb)
        monkeypatch.setattr(expiry, "cleanup_pod", MagicMock(side_effect=RuntimeError("boom")))
        freed = MagicMock()
        monkeypatch.setattr(expiry, "mark_disk_not_in_use", freed)

        reservation = {
            "reservation_id": "res-1",
            "user_id": "u@meta.com",
            "disk_name": "default",
            "pod_name": "gpu-dev-res1",
        }
        expiry.expire_reservation_due_to_dead_pod(reservation, "Pod failed (Evicted)")
        # a stuck in_use flag would permanently block the user, so it must still clear
        freed.assert_called_once_with("u@meta.com", "default")
