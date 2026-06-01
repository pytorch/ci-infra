"""Unit tests for index.try_claim_warm_pod (warm-pool fast-claim path).

Covers the fail-open contract (returns False on any miss/error so the caller
falls back to the cold path), the gpu_count == _warm_gpu_count gate, the GitHub
key requirement, pod graduation (app -> gpu-dev-pod, warm-* labels cleared), the
DDB record written with the real user_id/github_user, and the identity stamping
baked into the kubectl-exec inject command.

Everything that would touch the network / k8s / AWS is patched: get_k8s_client,
client.CoreV1Api, _list_warm_pods, stream, get_github_public_key, the async
trigger_* helpers, update_reservation_connection_info, and the boto3 handles via
the aws_mocks fixture.
"""
from unittest.mock import MagicMock, patch
from decimal import Decimal

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_pod(name="gpu-dev-h100-abc123", gpu_count=1, phase="Running",
              labels=None, annotations=None):
    """Build a fake kubernetes V1Pod-ish object with the attrs the code reads."""
    pod = MagicMock()
    pod.metadata.name = name
    base_labels = {
        "app": "gpu-dev-warm",
        "warm-state": "ready",
        "warm-gpu-type": "h100",
        "warm-gpu-count": str(gpu_count),
    }
    if labels is not None:
        base_labels.update(labels)
    pod.metadata.labels = base_labels
    pod.metadata.annotations = annotations if annotations is not None else {}
    pod.status.phase = phase
    return pod


def _body(**over):
    b = {
        "reservation_id": "res-123",
        "gpu_type": "h100",
        "gpu_count": 1,
        "github_user": "octocat",
        "user_id": "octocat-uid",
        "duration_hours": 8,
        "name": "my-box",
        "source_command": "reserve",
    }
    b.update(over)
    return b


class _Patches:
    """Context-manager bundle that patches every external dependency of
    try_claim_warm_pod and exposes the mocks for assertions."""

    def __init__(self, index, candidates, gh_key="ssh-rsa AAAAKEY user@host"):
        self.index = index
        self.candidates = candidates
        self.gh_key = gh_key
        self.v1 = MagicMock(name="v1")
        self._ps = []

    def __enter__(self):
        idx = self.index
        # build a fake SSH service whose first port carries the node_port
        svc = MagicMock()
        port = MagicMock()
        port.node_port = 31000
        svc.spec.ports = [port]
        self.v1.read_namespaced_service.return_value = svc

        starts = [
            patch.object(idx, "get_k8s_client", return_value=MagicMock(name="k8s")),
            patch.object(idx.client, "CoreV1Api", return_value=self.v1),
            patch.object(idx, "_list_warm_pods", return_value=self.candidates),
            patch.object(idx, "get_github_public_key", return_value=self.gh_key),
            patch.object(idx, "stream", return_value="") ,
            patch.object(idx, "trigger_warm_reconcile"),
            patch.object(idx, "trigger_efs_mount"),
            patch.object(idx, "trigger_availability_update"),
            patch.object(idx, "update_reservation_connection_info"),
            patch.object(idx, "get_pod_node_public_ip", return_value="1.2.3.4"),
            patch.object(idx, "get_pod_node_private_ip", return_value="10.0.0.5"),
            patch.object(idx, "get_pod_internal_ip", return_value="172.16.0.9"),
            patch.object(idx, "get_dns_enabled", return_value=False),
        ]
        self.mocks = {}
        for p in starts:
            m = p.start()
            self._ps.append(p)
        self.get_github_public_key = idx.get_github_public_key
        self.stream = idx.stream
        self.list_warm_pods = idx._list_warm_pods
        self.update_conn = idx.update_reservation_connection_info
        self.trigger_warm_reconcile = idx.trigger_warm_reconcile
        self.trigger_efs_mount = idx.trigger_efs_mount
        return self

    def __exit__(self, *exc):
        for p in reversed(self._ps):
            p.stop()
        return False


# ── _warm_gpu_count (the gate's other half) ──────────────────────────────────

def test_warm_gpu_count_cpu_is_zero(lambda_index):
    assert lambda_index._warm_gpu_count("cpu-x86") == 0
    assert lambda_index._warm_gpu_count("cpu-arm") == 0


def test_warm_gpu_count_gpu_is_one(lambda_index):
    assert lambda_index._warm_gpu_count("h100") == 1
    assert lambda_index._warm_gpu_count("b200") == 1
    assert lambda_index._warm_gpu_count("t4") == 1


# ── gpu_count gate ───────────────────────────────────────────────────────────

def test_gpu_count_mismatch_returns_false(lambda_index, aws_mocks):
    # 2 GPUs requested but a warm h100 pod only holds 1 -> immediate False,
    # no key lookup, no k8s client.
    with _Patches(lambda_index, [_make_pod()]) as P:
        assert try_count(lambda_index, _body(gpu_count=2)) is False
        P.get_github_public_key.assert_not_called()
        P.list_warm_pods.assert_not_called()


def test_gpu_count_matches_cpu_zero(lambda_index, aws_mocks):
    # cpu type wants 0 GPUs; body gpu_count 0 must pass the gate and proceed
    # (then succeed because there's a matching warm pod).
    pod = _make_pod(name="gpu-dev-cpu-x86-abc", gpu_count=0,
                    labels={"warm-gpu-type": "cpu-x86", "warm-gpu-count": "0"})
    with _Patches(lambda_index, [pod]):
        ok = try_count(lambda_index, _body(gpu_type="cpu-x86", gpu_count=0))
        assert ok is True


# ── github key gate ──────────────────────────────────────────────────────────

def test_no_github_key_returns_false(lambda_index, aws_mocks):
    with _Patches(lambda_index, [_make_pod()], gh_key="") as P:
        assert try_count(lambda_index, _body()) is False
        # key was looked up with the real github_user
        P.get_github_public_key.assert_called_once_with("octocat")
        # never proceeded to list pods
        P.list_warm_pods.assert_not_called()


# ── no candidate pod ─────────────────────────────────────────────────────────

def test_no_warm_pods_returns_false(lambda_index, aws_mocks):
    with _Patches(lambda_index, []):
        assert try_count(lambda_index, _body()) is False


def test_pod_not_running_returns_false(lambda_index, aws_mocks):
    with _Patches(lambda_index, [_make_pod(phase="Pending")]):
        assert try_count(lambda_index, _body()) is False


def test_pod_label_count_mismatch_returns_false(lambda_index, aws_mocks):
    # pod advertises a different warm-gpu-count than requested -> skipped
    pod = _make_pod(labels={"warm-gpu-count": "2"})
    with _Patches(lambda_index, [pod]):
        assert try_count(lambda_index, _body(gpu_count=1)) is False


# ── happy path: graduation, DDB write, identity stamping ─────────────────────

def test_successful_claim_returns_true(lambda_index, aws_mocks):
    with _Patches(lambda_index, [_make_pod()]):
        assert try_count(lambda_index, _body()) is True


def test_graduation_patch_clears_warm_labels(lambda_index, aws_mocks):
    pod = _make_pod(name="gpu-dev-h100-xyz")
    with _Patches(lambda_index, [pod]) as P:
        assert try_count(lambda_index, _body()) is True
        P.v1.patch_namespaced_pod.assert_called_once()
        args, kwargs = P.v1.patch_namespaced_pod.call_args
        assert args[0] == "gpu-dev-h100-xyz"
        assert args[1] == "gpu-dev"
        labels = args[2]["metadata"]["labels"]
        assert labels["app"] == "gpu-dev-pod"
        assert labels["warm-state"] is None
        assert labels["warm-gpu-type"] is None
        assert labels["warm-gpu-count"] is None
        anns = args[2]["metadata"]["annotations"]
        assert anns["gpu-dev-user-id"] == "octocat-uid"


def test_ddb_record_has_real_identity(lambda_index, aws_mocks):
    table = MagicMock(name="reservations_table")
    aws_mocks["dynamodb"].Table.return_value = table
    with _Patches(lambda_index, [_make_pod()]):
        assert try_count(lambda_index, _body()) is True
    table.put_item.assert_called_once()
    item = table.put_item.call_args.kwargs["Item"]
    assert item["reservation_id"] == "res-123"
    assert item["user_id"] == "octocat-uid"
    assert item["github_user"] == "octocat"
    assert item["gpu_type"] == "h100"
    assert item["gpu_count"] == 1
    assert item["status"] == "preparing"
    assert item["warm_claimed"] is True
    # duration is coerced to Decimal for DynamoDB
    assert item["duration_hours"] == Decimal("8.0")
    assert isinstance(item["duration_hours"], Decimal)


def test_ddb_record_default_name_when_missing(lambda_index, aws_mocks):
    table = MagicMock(name="reservations_table")
    aws_mocks["dynamodb"].Table.return_value = table
    body = _body()
    body.pop("name")
    with _Patches(lambda_index, [_make_pod()]):
        assert try_count(lambda_index, body) is True
    item = table.put_item.call_args.kwargs["Item"]
    assert item["name"] == "1x h100"


def test_inject_cmd_stamps_identity(lambda_index, aws_mocks):
    with _Patches(lambda_index, [_make_pod()]) as P:
        assert try_count(lambda_index, _body()) is True
        P.stream.assert_called_once()
        # the exec command is passed as the `command` kwarg
        cmd = P.stream.call_args.kwargs["command"]
        assert cmd[0] == "/bin/bash"
        assert cmd[1] == "-c"
        script = cmd[2]
        # real github key embedded
        assert "ssh-rsa AAAAKEY user@host" in script
        # identity stamped into managed shell-ext files
        assert 'GPU_DEV_USER_ID="octocat-uid"' in script
        assert 'GPU_DEV_GITHUB_USER="octocat"' in script
        assert 'AWS_ROLE_SESSION_NAME="octocat-uid"' in script
        assert 'GPU_DEV_RESERVATION_ID="res-123"' in script


def test_inject_cmd_targets_claimed_pod(lambda_index, aws_mocks):
    pod = _make_pod(name="gpu-dev-h100-target")
    with _Patches(lambda_index, [pod]) as P:
        assert try_count(lambda_index, _body()) is True
        # stream exec runs against the graduated pod in the gpu-dev namespace
        pos = P.stream.call_args.args
        assert pos[1] == "gpu-dev-h100-target"
        assert pos[2] == "gpu-dev"
        assert P.stream.call_args.kwargs["container"] == "gpu-dev"


def test_first_running_matching_pod_chosen(lambda_index, aws_mocks):
    # a Pending pod and a wrong-count pod precede the good one; the good one wins.
    bad_phase = _make_pod(name="gpu-dev-h100-pending", phase="Pending")
    bad_count = _make_pod(name="gpu-dev-h100-twocount",
                          labels={"warm-gpu-count": "2"})
    good = _make_pod(name="gpu-dev-h100-good")
    with _Patches(lambda_index, [bad_phase, bad_count, good]) as P:
        assert try_count(lambda_index, _body()) is True
        assert P.v1.patch_namespaced_pod.call_args.args[0] == "gpu-dev-h100-good"


# ── connection details from warm annotations (skip live k8s) ─────────────────

def test_uses_preprovisioned_annotations(lambda_index, aws_mocks):
    pod = _make_pod(annotations={
        "warm-node-port": "31999",
        "warm-domain": "-",
        "warm-node-ip": "9.9.9.9",
        "warm-node-private-ip": "10.9.9.9",
        "warm-pod-ip": "172.16.9.9",
    })
    with _Patches(lambda_index, [pod]) as P:
        assert try_count(lambda_index, _body()) is True
        # annotations present -> no live service read
        P.v1.read_namespaced_service.assert_not_called()
        P.update_conn.assert_called_once()
        kw = P.update_conn.call_args.kwargs
        assert kw["node_port"] == 31999
        assert kw["node_ip"] == "9.9.9.9"
        # warm-domain == "-" means no domain
        assert kw["domain_name"] is None
        # ssh_command falls back to host:port form
        assert kw["ssh_command"] == "ssh -p 31999 dev@9.9.9.9"


def test_resolves_live_when_no_annotations(lambda_index, aws_mocks):
    # no warm-* annotations -> reads the live service for the node port
    with _Patches(lambda_index, [_make_pod(annotations={})]) as P:
        assert try_count(lambda_index, _body()) is True
        P.v1.read_namespaced_service.assert_called_once_with(
            "gpu-dev-h100-abc123-ssh", "gpu-dev")
        kw = P.update_conn.call_args.kwargs
        assert kw["node_port"] == 31000
        assert kw["node_ip"] == "1.2.3.4"


# ── post-claim async triggers ────────────────────────────────────────────────

def test_async_triggers_fire_on_success(lambda_index, aws_mocks):
    with _Patches(lambda_index, [_make_pod()]) as P:
        assert try_count(lambda_index, _body()) is True
        P.trigger_warm_reconcile.assert_called_once()
        P.trigger_efs_mount.assert_called_once_with(
            "res-123", "octocat-uid", "gpu-dev-h100-abc123")


# ── fail-open: any internal error returns False ──────────────────────────────

def test_fail_open_on_k8s_error(lambda_index, aws_mocks):
    # get_k8s_client raising must be swallowed -> False (cold-path fallback)
    with patch.object(lambda_index, "get_k8s_client", side_effect=RuntimeError("boom")), \
         patch.object(lambda_index, "get_github_public_key", return_value="ssh-rsa K"):
        assert try_count(lambda_index, _body()) is False


def test_fail_open_on_patch_error(lambda_index, aws_mocks):
    with _Patches(lambda_index, [_make_pod()]) as P:
        P.v1.patch_namespaced_pod.side_effect = RuntimeError("patch failed")
        assert try_count(lambda_index, _body()) is False


def test_fail_open_on_ddb_error(lambda_index, aws_mocks):
    aws_mocks["dynamodb"].Table.side_effect = RuntimeError("ddb down")
    with _Patches(lambda_index, [_make_pod()]):
        assert try_count(lambda_index, _body()) is False


def test_fail_open_missing_gpu_type_key(lambda_index, aws_mocks):
    # gpu_type missing -> .startswith inside _warm_gpu_count raises AttributeError
    # which is caught by the outer try/except -> fail-open False.
    body = _body()
    body.pop("gpu_type")
    with _Patches(lambda_index, [_make_pod()]):
        assert try_count(lambda_index, body) is False


# ── helper to call the function by name (kept resilient to refactors) ─────────

def try_count(index, body):
    return index.try_claim_warm_pod(body)
