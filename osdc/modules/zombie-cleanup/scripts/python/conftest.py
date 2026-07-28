"""Shared fixtures for the zombie-cleanup unit tests."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from lightkube.core.exceptions import ApiError
from runner_busy import EphemeralRunner


def api_error(code: int) -> ApiError:
    """Build a lightkube ApiError carrying just a status code (no HTTP round-trip)."""
    err = ApiError.__new__(ApiError)
    err.status = MagicMock(code=code)
    return err


@pytest.fixture
def make_pod():
    """Factory for mock Pods with controllable metadata and status.

    ``labels`` is set to a real dict (or None) rather than an auto-speccing
    MagicMock so the busy gate's ``labels.get("runner-pod")`` behaves like a
    real pod instead of returning a truthy mock.
    """

    def _make(name, phase="Running", age_hours=0.0, owner_kind=None, terminating=False, labels=None):
        now = datetime.now(UTC)
        pod = MagicMock()
        pod.metadata.name = name
        pod.metadata.creationTimestamp = now - timedelta(hours=age_hours)
        pod.metadata.deletionTimestamp = now if terminating else None
        if owner_kind:
            ref = MagicMock()
            ref.kind = owner_kind
            pod.metadata.ownerReferences = [ref]
        else:
            pod.metadata.ownerReferences = None
        pod.metadata.labels = labels
        pod.status.phase = phase
        return pod

    return _make


@pytest.fixture
def make_er():
    """Factory for EphemeralRunner custom resources keyed by runner pod name.

    ``name`` MUST match the owning runner pod's name — that is the join key the
    busy gate uses. ``with_status=False`` produces a runner whose status block is
    absent (a freshly created EphemeralRunner).
    """

    def _make(name, job_id=None, with_status=True):
        body = {"metadata": {"name": name}}
        if with_status:
            body["status"] = {"jobId": job_id} if job_id is not None else {}
        return EphemeralRunner.from_dict(body)

    return _make


@pytest.fixture
def set_list():
    """Wire ``client.list`` and ``client.get`` for a full cleanup run.

    The find phase lists Pods then EphemeralRunners; the pre-delete recheck GETs
    one EphemeralRunner per candidate by name. ``client.list`` dispatches on the
    resource TYPE; ``client.get`` resolves an EphemeralRunner from ``ers`` by name
    and raises a 404 ApiError when absent (ER gone / anchor missing).

    Pass ``er_error`` to make BOTH the ER listing and the ER GETs raise (the API
    is down); pass ``get_error`` to fail only the recheck GETs.
    """

    def _set(client, pods=None, ers=None, er_error=None, get_error=None):
        pod_list = list(pods or [])
        er_list = list(ers or [])
        er_by_name = {er.metadata.name: er for er in er_list}

        def _dispatch(resource, *_args, **_kwargs):
            if resource is EphemeralRunner:
                if er_error is not None:
                    raise er_error
                return list(er_list)
            return list(pod_list)

        client.list.side_effect = _dispatch

        def _get(resource, *_args, name=None, **_kwargs):
            effective_error = get_error if get_error is not None else er_error
            if effective_error is not None:
                raise effective_error
            if resource is EphemeralRunner and name in er_by_name:
                return er_by_name[name]
            raise api_error(404)

        client.get.side_effect = _get

    return _set
