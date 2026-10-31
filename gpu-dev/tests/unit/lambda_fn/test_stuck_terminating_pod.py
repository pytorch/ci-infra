"""cleanup_pod must force-delete a pod that is already terminating.

A pod on a NotReady node keeps its deletionTimestamp forever (the kubelet can't
finish it), so re-issuing the 30s graceful delete is a no-op: the pod stays in
the list and every expiry run re-snapshots its disk (44k snapshots of one volume).
"""
import importlib.util
import pathlib
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

_EXPIRY = (
    pathlib.Path(__file__).resolve().parents[3]
    / "terraform-gpu-devservers" / "lambda" / "reservation_expiry" / "index.py"
)


@pytest.fixture
def expiry():
    spec = importlib.util.spec_from_file_location("expiry_index", _EXPIRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def api(expiry, monkeypatch):
    api = MagicMock()
    monkeypatch.setattr(expiry, "get_k8s_client", MagicMock())
    monkeypatch.setattr(expiry.client, "CoreV1Api", MagicMock(return_value=api))
    monkeypatch.setattr(expiry, "send_wall_message_to_pod", MagicMock())
    monkeypatch.setattr(expiry, "trigger_availability_update", MagicMock())
    return api


def _pod(deletion_timestamp):
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(deletion_timestamp=deletion_timestamp, labels={}),
        spec=types.SimpleNamespace(volumes=[]),
    )


def _grace(api):
    return api.delete_namespaced_pod.call_args.kwargs["grace_period_seconds"]


def test_running_pod_gets_graceful_delete(expiry, api):
    api.read_namespaced_pod.return_value = _pod(None)
    expiry.cleanup_pod("gpu-dev-040197fd")
    assert _grace(api) == 30


def test_already_terminating_pod_is_force_deleted(expiry, api):
    api.read_namespaced_pod.return_value = _pod(datetime(2026, 8, 3, 17, 18, 50, tzinfo=timezone.utc))
    expiry.cleanup_pod("gpu-dev-040197fd")
    assert _grace(api) == 0
