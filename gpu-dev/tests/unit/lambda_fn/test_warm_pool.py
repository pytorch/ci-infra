"""Unit tests for the reservation_processor warm-pool logic (index.py).

Focus (per task):
  * reconcile_warm_pool — claimed pods are NOT counted toward the target and are
    NOT recycled; Failed/Succeeded/stale ready pods ARE recycled; the deficit is
    topped up via _create_warm_pod.
  * _warm_gpu_count — 0 for cpu-* SKUs, 1 for everything else (MIG + full GPU).
  * _create_warm_pod — name shape `gpu-dev-<type>-<6hex>` with NO "warm" marker,
    create_pod called with warm=True / user_id="warm" / pytorch_ref="master",
    SSH service pre-created off the claim path, service failure swallowed.
  * _evict_warm_for_capacity — deletes the *minimum* warm-ready pods on a SINGLE
    node, guarded by a label re-check, returns (az, node) or (None, None).

Everything that would touch live k8s/AWS is mocked. The kubernetes high-level
client is `index.client`; reconcile_warm_pool does `client.CoreV1Api(get_k8s_client())`,
so we patch both `index.client` and `index.get_k8s_client`. The eviction helper
receives its `v1` directly (no patching of CoreV1Api needed there).
"""
import re
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# --- fake k8s object builders -------------------------------------------------

def _make_pod(name, *, warm_state="ready", phase="Running", node="node-a",
              gpu_type="h100", resource="nvidia.com/gpu", gpu_held=1,
              app="gpu-dev-warm", creation_ts=None, annotations=None,
              image_id=None):
    """A stand-in for a kubernetes V1Pod with just the attrs the code reads."""
    labels = {"app": app, "warm-gpu-type": gpu_type}
    if warm_state is not None:
        labels["warm-state"] = warm_state

    requests = {resource: str(gpu_held)} if gpu_held else {}
    container = SimpleNamespace(resources=SimpleNamespace(requests=requests))

    ts = None
    if creation_ts is not None:
        ts = SimpleNamespace(timestamp=lambda v=creation_ts: v)

    container_statuses = (
        [SimpleNamespace(name="gpu-dev", image_id=image_id)] if image_id else [])

    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, labels=labels, annotations=annotations or {},
            creation_timestamp=ts,
        ),
        spec=SimpleNamespace(node_name=node, containers=[container]),
        status=SimpleNamespace(phase=phase, container_statuses=container_statuses),
    )


def _node(name, free, az="us-east-2a"):
    return {"node_name": name, "az": az, "available_gpus": free}


# --- _warm_gpu_count ----------------------------------------------------------

@pytest.mark.parametrize("gpu_type,expected", [
    ("cpu-x86", 0),
    ("cpu-arm", 0),
    ("h100", 1),
    ("b200", 1),
    ("h100-mig-1g", 1),
    ("h100-mig-3g", 1),
    ("b200-mig-2g", 1),
    ("t4", 1),
])
def test_warm_gpu_count(lambda_index, gpu_type, expected):
    assert lambda_index._warm_gpu_count(gpu_type) == expected


def test_warm_gpu_count_only_cpu_prefix_is_zero(lambda_index):
    # the check is a startswith("cpu-"), so a type merely containing "cpu" is 1
    assert lambda_index._warm_gpu_count("h100cpu") == 1
    assert lambda_index._warm_gpu_count("cpu-anything") == 0


# --- _create_warm_pod ---------------------------------------------------------

_NAME_RE = re.compile(r"^gpu-dev-(?P<type>.+)-(?P<hex>[0-9a-f]{6})$")


def test_create_warm_pod_name_shape_and_no_warm_marker(lambda_index):
    with patch.object(lambda_index, "get_k8s_client", return_value=MagicMock()), \
         patch.object(lambda_index, "create_pod") as m_create_pod, \
         patch.object(lambda_index, "create_service") as m_create_svc, \
         patch.object(lambda_index, "find_available_node_port", return_value=31234):
        name = lambda_index._create_warm_pod("b200", 1)

    m = _NAME_RE.match(name)
    assert m, f"name {name!r} does not match gpu-dev-<type>-<6hex>"
    assert m.group("type") == "b200"
    # the deliberate design: the name must NOT advertise warmness
    assert "warm" not in name
    assert m_create_pod.called
    assert m_create_svc.called


def test_create_warm_pod_passes_warm_flags_to_create_pod(lambda_index):
    with patch.object(lambda_index, "get_k8s_client", return_value="K8S"), \
         patch.object(lambda_index, "create_pod") as m_create_pod, \
         patch.object(lambda_index, "create_service"), \
         patch.object(lambda_index, "find_available_node_port", return_value=31000):
        name = lambda_index._create_warm_pod("h100-mig-1g", 1)

    args, kwargs = m_create_pod.call_args
    # positional: (k8s_client, pod_name, gpu_count, gpu_type)
    assert args[0] == "K8S"
    assert args[1] == name
    assert args[2] == 1
    assert args[3] == "h100-mig-1g"
    assert kwargs["warm"] is True
    assert kwargs["user_id"] == "warm"
    assert kwargs["pytorch_ref"] == "master"
    assert kwargs["is_new_disk"] is True
    assert kwargs["github_public_key"] == ""


def test_create_warm_pod_service_failure_is_swallowed(lambda_index):
    # The SSH service is a fast-path optimization; if it fails the pod must still
    # be returned (the claim recreates/reuses the service).
    with patch.object(lambda_index, "get_k8s_client", return_value=MagicMock()), \
         patch.object(lambda_index, "create_pod"), \
         patch.object(lambda_index, "find_available_node_port",
                      side_effect=RuntimeError("no port")), \
         patch.object(lambda_index, "create_service") as m_create_svc:
        name = lambda_index._create_warm_pod("cpu-x86", 0)

    assert name.startswith("gpu-dev-cpu-x86-")
    # service creation never reached because port lookup blew up, but no raise
    m_create_svc.assert_not_called()


def test_create_warm_pod_uses_unique_names(lambda_index):
    with patch.object(lambda_index, "get_k8s_client", return_value=MagicMock()), \
         patch.object(lambda_index, "create_pod"), \
         patch.object(lambda_index, "create_service"), \
         patch.object(lambda_index, "find_available_node_port", return_value=31001):
        names = {lambda_index._create_warm_pod("h100", 1) for _ in range(20)}
    assert len(names) == 20  # uuid4 hex suffix → no collisions


# --- reconcile_warm_pool ------------------------------------------------------

def _reconcile_with(lambda_index, monkeypatch, *, pods_by_type, target_overrides=None,
                    desired_digest=None):
    """Run reconcile_warm_pool with a single fake CoreV1Api `v1`.

    pods_by_type: {gpu_type: [pods]} returned by _list_warm_pods for that type.
    Patches WARM_POOL_TARGETS to exactly the keys in target_overrides (so only
    the types we care about are reconciled). `desired_digest` stubs
    _current_image_digest (default None = image rotation disabled, so existing
    tests never touch ECR). Returns (v1, created_calls, result).
    """
    v1 = MagicMock(name="v1")

    def fake_list(_v1, gpu_type=None, state=None):
        items = pods_by_type.get(gpu_type, [])
        if state:
            items = [p for p in items if (p.metadata.labels or {}).get("warm-state") == state]
        return items

    created_calls = []

    def fake_create(gpu_type, gpu_count):
        created_calls.append((gpu_type, gpu_count))
        return f"gpu-dev-{gpu_type}-aaaaaa"

    monkeypatch.setattr(lambda_index, "WARM_POOL_TARGETS",
                        target_overrides or {"h100": 1})
    monkeypatch.setattr(lambda_index, "get_k8s_client", lambda: MagicMock())
    monkeypatch.setattr(lambda_index.client, "CoreV1Api", lambda _c: v1)
    monkeypatch.setattr(lambda_index, "_list_warm_pods", fake_list)
    monkeypatch.setattr(lambda_index, "_create_warm_pod", fake_create)
    monkeypatch.setattr(lambda_index, "_provision_warm_pod", lambda *_a, **_k: None)
    monkeypatch.setattr(lambda_index, "_current_image_digest",
                        lambda: desired_digest)

    result = lambda_index.reconcile_warm_pool()
    return v1, created_calls, result


def test_reconcile_creates_to_fill_deficit_from_empty(lambda_index, monkeypatch):
    v1, created, result = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"cpu-x86": []},
        target_overrides={"cpu-x86": 3},
    )
    assert created == [("cpu-x86", 0), ("cpu-x86", 0), ("cpu-x86", 0)]
    assert result["statusCode"] == 200


def test_reconcile_skips_claimed_pods_toward_target(lambda_index, monkeypatch):
    # Target is 2. There is 1 ready standby + 1 CLAIMED pod. The claimed pod is a
    # real reservation now and must NOT count toward the pool, so the reconciler
    # must create exactly 1 to refill (deficit = target - live = 2 - 1).
    ready = _make_pod("gpu-dev-h100-ready1", warm_state="ready", gpu_type="h100",
                      creation_ts=time.time())
    claimed = _make_pod("gpu-dev-h100-claimed1", warm_state="claimed", gpu_type="h100",
                        creation_ts=time.time())
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [ready, claimed]},
        target_overrides={"h100": 2},
    )
    assert created == [("h100", 1)]
    # claimed pod owns its own lifecycle: never recycled here
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert "gpu-dev-h100-claimed1" not in deleted
    assert "gpu-dev-h100-ready1" not in deleted


def test_reconcile_no_create_when_at_target(lambda_index, monkeypatch):
    a = _make_pod("gpu-dev-h100-a", warm_state="ready", creation_ts=time.time())
    b = _make_pod("gpu-dev-h100-b", warm_state="ready", creation_ts=time.time())
    _, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [a, b]},
        target_overrides={"h100": 2},
    )
    assert created == []


def test_reconcile_recycles_failed_and_succeeded(lambda_index, monkeypatch):
    failed = _make_pod("gpu-dev-h100-failed", warm_state="ready", phase="Failed",
                       creation_ts=time.time())
    succeeded = _make_pod("gpu-dev-h100-succeeded", warm_state="ready",
                          phase="Succeeded", creation_ts=time.time())
    v1, created, result = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [failed, succeeded]},
        target_overrides={"h100": 1},
    )
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert "gpu-dev-h100-failed" in deleted
    assert "gpu-dev-h100-succeeded" in deleted
    # both dead → live is empty → must refill the single target slot
    assert created == [("h100", 1)]
    # recycled count reported in body
    import json
    body = json.loads(result["body"])
    assert body["recycled"] == 2
    assert body["created"] == 1


def test_reconcile_recycles_stale_ready_pod(lambda_index, monkeypatch):
    # ready pod older than WARM_POD_MAX_AGE_HOURS is recycled (anti-rot).
    monkeypatch.setattr(lambda_index, "WARM_POD_MAX_AGE_HOURS", 1.0)
    old_ts = time.time() - (2 * 3600)  # 2h old > 1h max
    fresh_ts = time.time()
    stale = _make_pod("gpu-dev-h100-stale", warm_state="ready", creation_ts=old_ts)
    fresh = _make_pod("gpu-dev-h100-fresh", warm_state="ready", creation_ts=fresh_ts)
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [stale, fresh]},
        target_overrides={"h100": 2},
    )
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert "gpu-dev-h100-stale" in deleted
    assert "gpu-dev-h100-fresh" not in deleted
    # stale removed → live = 1 (fresh) → 1 to refill
    assert created == [("h100", 1)]


def test_reconcile_does_not_recycle_stale_claimed(lambda_index, monkeypatch):
    # A claimed pod that is "old" is a long-running reservation, NOT stale standby.
    monkeypatch.setattr(lambda_index, "WARM_POD_MAX_AGE_HOURS", 1.0)
    old_ts = time.time() - (10 * 3600)
    claimed = _make_pod("gpu-dev-h100-longrun", warm_state="claimed", creation_ts=old_ts)
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [claimed]},
        target_overrides={"h100": 1},
    )
    v1.delete_namespaced_pod.assert_not_called()
    # claimed not counted as live → still need to create the 1 standby
    assert created == [("h100", 1)]


def test_reconcile_provisions_ready_pods(lambda_index, monkeypatch):
    ready = _make_pod("gpu-dev-h100-r", warm_state="ready", creation_ts=time.time())
    provisioned = []
    v1 = MagicMock(name="v1")
    monkeypatch.setattr(lambda_index, "WARM_POOL_TARGETS", {"h100": 1})
    monkeypatch.setattr(lambda_index, "get_k8s_client", lambda: MagicMock())
    monkeypatch.setattr(lambda_index.client, "CoreV1Api", lambda _c: v1)
    monkeypatch.setattr(lambda_index, "_list_warm_pods",
                        lambda _v, gpu_type=None, state=None: [ready])
    monkeypatch.setattr(lambda_index, "_create_warm_pod", lambda *a: "x")
    monkeypatch.setattr(lambda_index, "_provision_warm_pod",
                        lambda _v, p: provisioned.append(p.metadata.name))
    lambda_index.reconcile_warm_pool()
    assert provisioned == ["gpu-dev-h100-r"]


def test_reconcile_per_type_failure_isolated(lambda_index, monkeypatch):
    # A blow-up reconciling one type must not stop the others (outer try/except
    # per gpu_type). Make _list_warm_pods raise for cpu-arm only.
    def fake_list(_v, gpu_type=None, state=None):
        if gpu_type == "cpu-arm":
            raise RuntimeError("boom")
        return []
    v1 = MagicMock()
    created = []
    monkeypatch.setattr(lambda_index, "WARM_POOL_TARGETS", {"cpu-arm": 2, "cpu-x86": 2})
    monkeypatch.setattr(lambda_index, "get_k8s_client", lambda: MagicMock())
    monkeypatch.setattr(lambda_index.client, "CoreV1Api", lambda _c: v1)
    monkeypatch.setattr(lambda_index, "_list_warm_pods", fake_list)
    monkeypatch.setattr(lambda_index, "_create_warm_pod",
                        lambda gt, gc: created.append(gt))
    monkeypatch.setattr(lambda_index, "_provision_warm_pod", lambda *a, **k: None)
    result = lambda_index.reconcile_warm_pool()
    assert result["statusCode"] == 200
    # cpu-x86 still got topped up despite cpu-arm failing
    assert created.count("cpu-x86") == 2
    assert "cpu-arm" not in created


# --- _recycle_warm_pod: domain-mapping cleanup (orphan-leak fix) --------------

def test_recycle_deletes_domain_mapping(lambda_index, monkeypatch):
    # The whole point of the fix: recycling a warm pod must also free its
    # placeholder DNS name, else generate_unique_name() collides against orphans.
    deleted = []
    monkeypatch.setattr(lambda_index, "delete_domain_mapping",
                        lambda d: deleted.append(d))
    v1 = MagicMock()
    pod = _make_pod("gpu-dev-h100-x", annotations={"warm-domain": "clever_fox"})
    assert lambda_index._recycle_warm_pod(v1, pod) is True
    v1.delete_namespaced_pod.assert_called_once()
    assert deleted == ["clever_fox"]


def test_recycle_no_mapping_delete_when_no_domain(lambda_index, monkeypatch):
    deleted = []
    monkeypatch.setattr(lambda_index, "delete_domain_mapping",
                        lambda d: deleted.append(d))
    v1 = MagicMock()
    # placeholder "-" and absent annotation must both skip the mapping delete
    for ann in ({"warm-domain": "-"}, {}):
        pod = _make_pod("gpu-dev-h100-x", annotations=ann)
        assert lambda_index._recycle_warm_pod(v1, pod) is True
    assert deleted == []


def test_recycle_swallows_mapping_delete_error(lambda_index, monkeypatch):
    # A failed mapping cleanup must not fail the recycle (pod is already gone).
    monkeypatch.setattr(lambda_index, "delete_domain_mapping",
                        lambda d: (_ for _ in ()).throw(RuntimeError("ddb down")))
    v1 = MagicMock()
    pod = _make_pod("gpu-dev-h100-x", annotations={"warm-domain": "clever_fox"})
    assert lambda_index._recycle_warm_pod(v1, pod) is True


# --- _provision_warm_pod: placeholder mapping expiry --------------------------

def test_provision_uses_short_placeholder_expiry(lambda_index, monkeypatch):
    # The placeholder must expire on the pod's own timescale (max_age + 2h), NOT
    # the old 7 days — that long TTL is what let orphans pile up.
    monkeypatch.setattr(lambda_index, "WARM_POD_MAX_AGE_HOURS", 12.0)
    monkeypatch.setattr(lambda_index, "get_dns_enabled", lambda: True)
    monkeypatch.setattr(lambda_index, "generate_unique_name", lambda: "brave_owl")
    monkeypatch.setattr(lambda_index, "get_pod_node_public_ip", lambda _n: "1.2.3.4")
    monkeypatch.setattr(lambda_index, "get_pod_node_private_ip", lambda _n: "10.0.0.1")
    monkeypatch.setattr(lambda_index, "get_pod_internal_ip", lambda _n: "10.1.0.1")
    captured = {}
    monkeypatch.setattr(lambda_index, "store_domain_mapping",
                        lambda *a: captured.setdefault("args", a))
    v1 = MagicMock()
    v1.read_namespaced_service.return_value = SimpleNamespace(
        spec=SimpleNamespace(ports=[SimpleNamespace(node_port=30111)]))
    pod = _make_pod("gpu-dev-h100-fresh", warm_state="ready")

    before = time.time()
    lambda_index._provision_warm_pod(v1, pod)
    expires = captured["args"][4]
    # (12h + 2h) ahead, well under the old 7-day (604800s) value
    assert 13 * 3600 < (expires - before) < 15 * 3600


# --- image digest helpers + rotation ------------------------------------------

def test_pod_image_digest_parses_image_id(lambda_index):
    pod = _make_pod("p", image_id="acct.dkr.ecr.us-east-2.amazonaws.com/repo@sha256:abc123")
    assert lambda_index._pod_image_digest(pod) == "sha256:abc123"


def test_pod_image_digest_none_when_absent(lambda_index):
    assert lambda_index._pod_image_digest(_make_pod("p")) is None  # no statuses


def test_current_image_digest_queries_ecr(lambda_index, monkeypatch):
    fake_ecr = MagicMock()
    fake_ecr.describe_images.return_value = {
        "imageDetails": [{"imageDigest": "sha256:deadbeef"}]}
    monkeypatch.setattr(lambda_index, "GPU_DEV_CONTAINER_IMAGE",
                        "1234.dkr.ecr.us-east-2.amazonaws.com/gpu-dev:latest")
    monkeypatch.setattr(lambda_index.boto3, "client", lambda svc: fake_ecr)
    assert lambda_index._current_image_digest() == "sha256:deadbeef"
    _, kw = fake_ecr.describe_images.call_args
    assert kw["repositoryName"] == "gpu-dev"
    assert kw["imageIds"] == [{"imageTag": "latest"}]


def test_current_image_digest_none_for_non_ecr_image(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "GPU_DEV_CONTAINER_IMAGE",
                        "pytorch/pytorch:2.11.0-cuda12.8-cudnn9-devel")
    assert lambda_index._current_image_digest() is None


def test_current_image_digest_swallows_ecr_error(lambda_index, monkeypatch):
    def boom(_svc):
        raise RuntimeError("no ecr")
    monkeypatch.setattr(lambda_index, "GPU_DEV_CONTAINER_IMAGE",
                        "1234.dkr.ecr.us-east-2.amazonaws.com/gpu-dev:latest")
    monkeypatch.setattr(lambda_index.boto3, "client", boom)
    assert lambda_index._current_image_digest() is None


def test_reconcile_rotates_one_stale_image_pod(lambda_index, monkeypatch):
    # Two ready pods at target=2: one on the desired digest, one stale. The stale
    # one is recycled (and refilled); the current one is left alone.
    cur = _make_pod("gpu-dev-h100-current", warm_state="ready", creation_ts=time.time(),
                    image_id="r@sha256:NEW")
    stale = _make_pod("gpu-dev-h100-stale", warm_state="ready", creation_ts=time.time(),
                      image_id="r@sha256:OLD", annotations={"warm-domain": "old_fox"})
    monkeypatch.setattr(lambda_index, "delete_domain_mapping", lambda d: None)
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [cur, stale]},
        target_overrides={"h100": 2},
        desired_digest="sha256:NEW",
    )
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert "gpu-dev-h100-stale" in deleted
    assert "gpu-dev-h100-current" not in deleted
    assert created == [("h100", 1)]  # the rotated-out slot is refilled


def test_reconcile_rotation_caps_one_per_tick(lambda_index, monkeypatch):
    # Three stale ready pods, target=3: only ONE is rotated per reconcile (gradual).
    stale = [_make_pod(f"gpu-dev-h100-s{i}", warm_state="ready", creation_ts=time.time(),
                       image_id="r@sha256:OLD") for i in range(3)]
    monkeypatch.setattr(lambda_index, "delete_domain_mapping", lambda d: None)
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": stale},
        target_overrides={"h100": 3},
        desired_digest="sha256:NEW",
    )
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert len(deleted) == 1
    assert created == [("h100", 1)]


def test_reconcile_no_rotation_when_digest_matches(lambda_index, monkeypatch):
    p = _make_pod("gpu-dev-h100-ok", warm_state="ready", creation_ts=time.time(),
                  image_id="r@sha256:NEW")
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [p]},
        target_overrides={"h100": 1},
        desired_digest="sha256:NEW",
    )
    v1.delete_namespaced_pod.assert_not_called()
    assert created == []


def test_reconcile_skips_rotation_when_digest_unknown(lambda_index, monkeypatch):
    # ECR lookup failed (None) -> never recycle on uncertainty.
    p = _make_pod("gpu-dev-h100-x", warm_state="ready", creation_ts=time.time(),
                  image_id="r@sha256:OLD")
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [p]},
        target_overrides={"h100": 1},
        desired_digest=None,
    )
    v1.delete_namespaced_pod.assert_not_called()
    assert created == []


def test_reconcile_rotation_never_touches_claimed(lambda_index, monkeypatch):
    # A claimed pod on a stale image is a real reservation — never rotate it.
    claimed = _make_pod("gpu-dev-h100-claimed", warm_state="claimed",
                        creation_ts=time.time(), image_id="r@sha256:OLD")
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"h100": [claimed]},
        target_overrides={"h100": 1},
        desired_digest="sha256:NEW",
    )
    v1.delete_namespaced_pod.assert_not_called()
    assert created == [("h100", 1)]  # claimed not counted; refill the standby slot


# --- _evict_warm_for_capacity -------------------------------------------------

def _patch_resource_name(lambda_index, monkeypatch, name="nvidia.com/gpu"):
    monkeypatch.setattr(lambda_index, "get_gpu_resource_name", lambda _gt: name)


def test_evict_returns_none_when_no_warm_pods(lambda_index, monkeypatch):
    _patch_resource_name(lambda_index, monkeypatch)
    monkeypatch.setattr(lambda_index, "_list_warm_pods",
                        lambda *a, **k: [])
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 2, [_node("node-a", 0)])
    assert (az, node) == (None, None)
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_minimum_pods_on_single_node(lambda_index, monkeypatch):
    # node-a has 0 free + 4 single-GPU warm pods. Request needs 2 GPUs → evict
    # exactly 2 (the minimum), not all 4.
    _patch_resource_name(lambda_index, monkeypatch)
    warm = [_make_pod(f"gpu-dev-h100-w{i}", node="node-a", gpu_held=1)
            for i in range(4)]
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 2, [_node("node-a", 0)])
    assert (az, node) == ("us-east-2a", "node-a")
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert len(deleted) == 2  # minimum to satisfy 2 GPUs
    # the ssh service is best-effort deleted alongside
    assert v1.delete_namespaced_service.call_count == 2


def test_evict_accounts_for_already_free_gpus(lambda_index, monkeypatch):
    # node already has 1 free + 3 warm pods, request needs 2 → evict only 1.
    _patch_resource_name(lambda_index, monkeypatch)
    warm = [_make_pod(f"gpu-dev-h100-w{i}", node="node-a", gpu_held=1)
            for i in range(3)]
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 2, [_node("node-a", 1)])
    assert node == "node-a"
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert len(deleted) == 1


def test_evict_returns_none_when_node_cannot_satisfy(lambda_index, monkeypatch):
    # Only 1 warm pod (1 GPU) on a 0-free node, but request needs 4 → can't be
    # satisfied on a single node → no deletions, (None, None).
    _patch_resource_name(lambda_index, monkeypatch)
    warm = [_make_pod("gpu-dev-h100-w0", node="node-a", gpu_held=1)]
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 4, [_node("node-a", 0)])
    assert (az, node) == (None, None)
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_single_node_constraint(lambda_index, monkeypatch):
    # 1 warm pod on each of two nodes, request needs 2. No single node can free 2,
    # so eviction must NOT spread across nodes → (None, None).
    _patch_resource_name(lambda_index, monkeypatch)
    warm = [
        _make_pod("gpu-dev-h100-a", node="node-a", gpu_held=1),
        _make_pod("gpu-dev-h100-b", node="node-b", gpu_held=1),
    ]
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 2, [_node("node-a", 0), _node("node-b", 0)])
    assert (az, node) == (None, None)
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_guard_skips_non_idle_warm_pod(lambda_index, monkeypatch):
    # Hard guard: even if _list_warm_pods (mocked) yields a pod whose label is NOT
    # an idle ready standby, the pre-delete re-check must skip deleting it.
    _patch_resource_name(lambda_index, monkeypatch)
    racy = _make_pod("gpu-dev-h100-racy", node="node-a", gpu_held=1,
                     warm_state="claimed")  # not "ready" → must be skipped
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: [racy])
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 1, [_node("node-a", 0)])
    # the node was selected (freed >= requested via the held accounting), but the
    # guard prevents the actual delete of the non-idle pod
    v1.delete_namespaced_pod.assert_not_called()
    assert (az, node) == ("us-east-2a", "node-a")


def test_evict_guard_skips_wrong_app_label(lambda_index, monkeypatch):
    _patch_resource_name(lambda_index, monkeypatch)
    wrong = _make_pod("gpu-dev-h100-wrong", node="node-a", gpu_held=1,
                      app="gpu-dev", warm_state="ready")  # app != gpu-dev-warm
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: [wrong])
    v1 = MagicMock()
    lambda_index._evict_warm_for_capacity(
        v1, "h100", 1, [_node("node-a", 0)])
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_ignores_non_running_and_unscheduled(lambda_index, monkeypatch):
    # Pending (no node) and non-Running pods are not eviction candidates.
    _patch_resource_name(lambda_index, monkeypatch)
    pending = _make_pod("gpu-dev-h100-pending", node=None, gpu_held=1)
    notrunning = _make_pod("gpu-dev-h100-stopping", node="node-a",
                           phase="Terminating", gpu_held=1)
    monkeypatch.setattr(lambda_index, "_list_warm_pods",
                        lambda *a, **k: [pending, notrunning])
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 1, [_node("node-a", 0)])
    assert (az, node) == (None, None)
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_delete_error_swallowed_returns_node(lambda_index, monkeypatch):
    # If the delete call itself errors, eviction logs + still returns the node
    # (best-effort; reconciler reconverges). The whole helper is fail-soft.
    _patch_resource_name(lambda_index, monkeypatch)
    warm = [_make_pod("gpu-dev-h100-w0", node="node-a", gpu_held=1)]
    monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
    v1 = MagicMock()
    v1.delete_namespaced_pod.side_effect = RuntimeError("api down")
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 1, [_node("node-a", 0)])
    assert (az, node) == ("us-east-2a", "node-a")


def test_evict_outer_exception_returns_none(lambda_index, monkeypatch):
    # get_gpu_resource_name raising → whole helper returns (None, None).
    monkeypatch.setattr(lambda_index, "get_gpu_resource_name",
                        MagicMock(side_effect=RuntimeError("nope")))
    v1 = MagicMock()
    az, node = lambda_index._evict_warm_for_capacity(
        v1, "h100", 1, [_node("node-a", 0)])
    assert (az, node) == (None, None)


# --- reconcile_warm_pool: scale-down + de-targeted cleanup --------------------

def test_reconcile_scales_down_when_over_target(lambda_index, monkeypatch):
    # Target lowered to 2 but 5 ready standby pods exist -> delete the 3 excess.
    pods = [_make_pod(f"gpu-dev-cpu-x86-{i}", warm_state="ready", gpu_type="cpu-x86",
                      gpu_held=0, creation_ts=time.time()) for i in range(5)]
    v1, created, result = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"cpu-x86": pods, None: pods},
        target_overrides={"cpu-x86": 2},
    )
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert len(deleted) == 3, deleted          # 5 live - 2 target
    assert created == []                         # already over target
    import json
    assert json.loads(result["body"])["recycled"] == 3


def test_reconcile_cleans_up_detargeted_types(lambda_index, monkeypatch):
    # Targets = {t4:1}. Stale h100 standby pods (a type no longer targeted, e.g.
    # left over from the default targets on a cluster with no h100 nodes) must be
    # deleted; a CLAIMED h100 (real reservation) must NOT be touched.
    t4 = _make_pod("gpu-dev-t4-x", warm_state="ready", gpu_type="t4", creation_ts=time.time())
    h1 = _make_pod("gpu-dev-h100-a", warm_state="ready", gpu_type="h100", creation_ts=time.time())
    h2 = _make_pod("gpu-dev-h100-b", warm_state="ready", gpu_type="h100", creation_ts=time.time())
    hclaim = _make_pod("gpu-dev-h100-claimed", warm_state="claimed", gpu_type="h100",
                       creation_ts=time.time())
    allpods = [t4, h1, h2, hclaim]
    v1, created, _ = _reconcile_with(
        lambda_index, monkeypatch,
        pods_by_type={"t4": [t4], None: allpods},
        target_overrides={"t4": 1},
    )
    deleted = [c.args[0] for c in v1.delete_namespaced_pod.call_args_list]
    assert "gpu-dev-h100-a" in deleted and "gpu-dev-h100-b" in deleted   # de-targeted -> cleaned
    assert "gpu-dev-h100-claimed" not in deleted                         # real reservation, untouched
    assert "gpu-dev-t4-x" not in deleted                                 # targeted type kept
    assert created == []                                                 # t4 already at target
