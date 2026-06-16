"""Unit tests for the MIG / GPU-type config helpers in the reservation_processor lambda.

Covers the pure, side-effect-free mapping/aggregation logic:

  * GPU_CONFIG single-source-of-truth invariants for MIG vs full SKUs
  * get_gpu_resource_name / get_node_gpu_type   (SKU -> k8s resource / node label)
  * _mig_slice_fraction                          (1g..7g -> GPC fraction, 1.0 for full)
  * get_pod_resource_limits / get_pod_resource_requests  (MIG-fraction CPU/mem sizing,
    full-GPU proportional sizing, EFA gating that EXCLUDES MIG, cpu-* branch)
  * _pod_uses_efa                                (full-vs-MIG-vs-multinode gate)
  * get_available_gpus_on_node                   (allocatable - in-use slices, error -> 0)
  * get_instance_type_and_gpu_info               (MIG resource-request -> SKU; full -> mapping)
  * get_target_az_for_reservation                (binpacking, warm eviction gating, fallbacks)
  * _evict_warm_for_capacity                     (minimum-evict-on-one-node, label guard)

All k8s/AWS access is patched (get_k8s_client, client.CoreV1Api, _list_warm_pods,
get_available_gpus_on_node, _evict_warm_for_capacity) — no network, no real cluster.
"""
from unittest.mock import MagicMock, patch

import pytest


# ── fake k8s object builders ──────────────────────────────────────────────────

def _node(name, az="us-east-2a", ready=True, schedulable=True,
          allocatable=None, zone_label="topology.kubernetes.io/zone"):
    n = MagicMock()
    n.metadata.name = name
    labels = {}
    if az is not None:
        labels[zone_label] = az
    n.metadata.labels = labels
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True" if ready else "False"
    n.status.conditions = [cond]
    n.spec.unschedulable = not schedulable
    n.status.allocatable = allocatable if allocatable is not None else {}
    return n


def _pod_with_request(resource, count, phase="Running"):
    p = MagicMock()
    p.status.phase = phase
    c = MagicMock()
    c.resources.requests = {resource: str(count)}
    p.spec.containers = [c]
    return p


def _warm_pod(name, node, held=1, phase="Running",
              labels=None, resource="nvidia.com/gpu"):
    p = MagicMock()
    p.metadata.name = name
    p.metadata.labels = labels if labels is not None else {
        "app": "gpu-dev-warm", "warm-state": "ready"}
    p.spec.node_name = node
    c = MagicMock()
    c.resources.requests = {resource: str(held)}
    p.spec.containers = [c]
    p.status.phase = phase
    return p


# ── GPU_CONFIG invariants ─────────────────────────────────────────────────────

def test_mig_skus_carry_k8s_resource_and_node_gpu_type(lambda_index):
    cfg = lambda_index.GPU_CONFIG
    for sku in ("h100-mig-1g", "h100-mig-2g", "h100-mig-3g",
                "b200-mig-1g", "b200-mig-2g", "b200-mig-3g"):
        assert "mig" in cfg[sku]["k8s_resource"]
        # MIG SKU's node label maps to the underlying physical card, not the SKU
        assert cfg[sku]["node_gpu_type"] in ("h100", "b200")
        assert cfg[sku]["node_gpu_type"] != sku


def test_full_gpu_skus_have_no_k8s_resource_override(lambda_index):
    # full GPUs default to nvidia.com/gpu (no explicit k8s_resource key)
    for sku in ("h100", "b200", "t4", "a100"):
        assert "k8s_resource" not in lambda_index.GPU_CONFIG[sku]


def test_h100_mig_1g_max_gpus_is_16(lambda_index):
    # 8 cards x 2 1g-slices each on the all-balanced p5 profile
    assert lambda_index.GPU_CONFIG["h100-mig-1g"]["max_gpus"] == 16
    assert lambda_index.GPU_CONFIG["h100-mig-2g"]["max_gpus"] == 8


# ── get_gpu_resource_name ─────────────────────────────────────────────────────

def test_resource_name_full_gpu_default(lambda_index):
    assert lambda_index.get_gpu_resource_name("h100") == "nvidia.com/gpu"
    assert lambda_index.get_gpu_resource_name("t4") == "nvidia.com/gpu"


def test_resource_name_mig(lambda_index):
    assert lambda_index.get_gpu_resource_name("h100-mig-1g") == "nvidia.com/mig-1g.10gb"
    assert lambda_index.get_gpu_resource_name("b200-mig-2g") == "nvidia.com/mig-2g.45gb"


def test_resource_name_unknown_falls_back_to_default(lambda_index):
    # unknown SKU -> GPU_CONFIG_DEFAULT, which has no k8s_resource -> nvidia.com/gpu
    assert lambda_index.get_gpu_resource_name("does-not-exist") == "nvidia.com/gpu"


def test_resource_name_cpu_type(lambda_index):
    # cpu-* has no k8s_resource override
    assert lambda_index.get_gpu_resource_name("cpu-x86") == "nvidia.com/gpu"


# ── get_node_gpu_type ─────────────────────────────────────────────────────────

def test_node_gpu_type_full_is_identity(lambda_index):
    assert lambda_index.get_node_gpu_type("h100") == "h100"
    assert lambda_index.get_node_gpu_type("b200") == "b200"


def test_node_gpu_type_mig_maps_to_physical(lambda_index):
    assert lambda_index.get_node_gpu_type("h100-mig-3g") == "h100"
    assert lambda_index.get_node_gpu_type("b200-mig-1g") == "b200"


def test_node_gpu_type_unknown_returns_input(lambda_index):
    # not in GPU_CONFIG -> default {} -> .get(node_gpu_type, gpu_type) returns input
    assert lambda_index.get_node_gpu_type("mystery") == "mystery"


# ── _mig_slice_fraction ───────────────────────────────────────────────────────

def test_mig_fraction_non_mig_is_one(lambda_index):
    assert lambda_index._mig_slice_fraction("h100") == 1.0
    assert lambda_index._mig_slice_fraction("cpu-x86") == 1.0
    assert lambda_index._mig_slice_fraction("t4") == 1.0


@pytest.mark.parametrize("sku,slices", [
    ("h100-mig-1g", 1), ("h100-mig-2g", 2), ("h100-mig-3g", 3),
    ("b200-mig-1g", 1), ("b200-mig-2g", 2), ("b200-mig-3g", 3),
])
def test_mig_fraction_is_slices_over_seven(lambda_index, sku, slices):
    assert lambda_index._mig_slice_fraction(sku) == pytest.approx(slices / 7.0)


def test_mig_fraction_7g_is_full_gpu(lambda_index):
    assert lambda_index._mig_slice_fraction("h100-mig-7g") == pytest.approx(1.0)


def test_mig_fraction_malformed_returns_one(lambda_index):
    # "-mig-" present but no integer before 'g' -> ValueError -> 1.0 fallback
    assert lambda_index._mig_slice_fraction("weird-mig-g") == 1.0
    # contains 'mig' but not the expected '-mig-' split -> IndexError -> 1.0
    assert lambda_index._mig_slice_fraction("migxyz") == 1.0


# ── get_pod_resource_limits: MIG sizing ───────────────────────────────────────

def test_limits_mig_1g_uses_gpc_fraction(lambda_index):
    # h100: cpus=192, mem=2048, full_gpus_per_node=8 -> per-full-gpu cpu=24 mem=256
    # 1g slice fraction = 1/7. limit cpu = max(1,min(192,int(24*(1/7)*1*1.5)))=5
    # limit mem = max(1,int(256*(1/7)*1)) = 36
    lim = lambda_index.get_pod_resource_limits(1, "h100-mig-1g")
    assert lim["nvidia.com/mig-1g.10gb"] == "1"
    assert lim["cpu"] == "5"
    assert lim["memory"] == "36Gi"


def test_limits_mig_2g_two_slices(lambda_index):
    # 2g fraction=2/7, gpu_count=2: cpu=int(24*(2/7)*2*1.5)=20, mem=int(256*(2/7)*2)=146
    lim = lambda_index.get_pod_resource_limits(2, "h100-mig-2g")
    assert lim["nvidia.com/mig-2g.20gb"] == "2"
    assert lim["cpu"] == "20"
    assert lim["memory"] == "146Gi"


def test_requests_mig_1g_uses_90pct_of_fraction(lambda_index):
    # request cpu = max(1,int(24*(1/7)*1*0.9)) = 3 ; mem = max(1,int(256*(1/7)*1*0.9)) = 32
    req = lambda_index.get_pod_resource_requests(1, "h100-mig-1g")
    assert req["nvidia.com/mig-1g.10gb"] == "1"
    assert req["cpu"] == "3"
    assert req["memory"] == "32Gi"


def test_mig_request_floor_is_one(lambda_index):
    # b200-mig-1g (fraction 1/7) on a tiny config can't underflow to 0 — floored at 1
    req = lambda_index.get_pod_resource_requests(1, "b200-mig-1g")
    assert int(req["cpu"]) >= 1
    assert int(req["memory"].rstrip("Gi")) >= 1


# ── get_pod_resource_limits: full-GPU proportional sizing ─────────────────────

def test_limits_full_gpu_proportional(lambda_index):
    # h100 4/8 GPUs: ratio 0.5; cpu = min(192, int(192*0.5*1.5)) = 144 ; mem = int(2048*0.5)=1024
    lim = lambda_index.get_pod_resource_limits(4, "h100")
    assert lim["nvidia.com/gpu"] == "4"
    assert lim["cpu"] == "144"
    assert lim["memory"] == "1024Gi"


def test_limits_full_gpu_cpu_capped_at_node_cpus(lambda_index):
    # 8/8 GPUs: int(192*1.0*1.5)=288 but min(config cpus=192) caps it at 192
    lim = lambda_index.get_pod_resource_limits(8, "h100")
    assert lim["cpu"] == "192"
    assert lim["memory"] == "2048Gi"


# ── cpu-* branch ──────────────────────────────────────────────────────────────

def test_limits_cpu_type_whole_node(lambda_index):
    # cpu-x86: cpus=32, mem=64 -> cpu = int(32*0.85)=27, memory = int(64*0.80)=51Gi
    lim = lambda_index.get_pod_resource_limits(0, "cpu-x86")
    assert lim["cpu"] == "27"
    assert lim["memory"] == "51Gi"
    assert "nvidia.com/gpu" not in lim


def test_requests_cpu_type_equals_limits(lambda_index):
    # request == limit (whole-node Guaranteed-style sizing, no co-tenant eviction)
    assert lambda_index.get_pod_resource_requests(0, "cpu-arm") == \
        lambda_index.get_pod_resource_limits(0, "cpu-arm")


# ── EFA gating: MIG must NOT get EFA even at "full" slice count ────────────────

def test_limits_mig_full_node_skips_efa(lambda_index):
    # multinode + gpu_count==max_gpus(8) for a MIG SKU -> still NO EFA (mig excluded)
    lim = lambda_index.get_pod_resource_limits(8, "h100-mig-2g", is_multinode=True)
    assert "vpc.amazonaws.com/efa" not in lim
    assert "hugepages-2Mi" not in lim


def test_limits_full_gpu_multinode_gets_efa(lambda_index):
    # h100 8/8 multinode -> EFA with config efa_count (32)
    lim = lambda_index.get_pod_resource_limits(8, "h100", is_multinode=True)
    assert lim["vpc.amazonaws.com/efa"] == "32"
    assert lim["hugepages-2Mi"] == "5120Mi"


def test_limits_full_gpu_singlenode_no_efa(lambda_index):
    lim = lambda_index.get_pod_resource_limits(8, "h100", is_multinode=False)
    assert "vpc.amazonaws.com/efa" not in lim


def test_requests_full_gpu_multinode_gets_efa(lambda_index):
    req = lambda_index.get_pod_resource_requests(8, "h100", is_multinode=True)
    assert req["vpc.amazonaws.com/efa"] == "32"


def test_limits_t4_small_excluded_from_efa(lambda_index):
    # t4-small is explicitly excluded from EFA even when it'd otherwise qualify
    lim = lambda_index.get_pod_resource_limits(1, "t4-small", is_multinode=True)
    assert "vpc.amazonaws.com/efa" not in lim


# ── _pod_uses_efa ─────────────────────────────────────────────────────────────

def test_pod_uses_efa_full_node_multinode(lambda_index):
    assert lambda_index._pod_uses_efa(8, "h100", is_multinode=True) is True


def test_pod_uses_efa_false_partial(lambda_index):
    assert lambda_index._pod_uses_efa(4, "h100", is_multinode=True) is False


def test_pod_uses_efa_false_singlenode(lambda_index):
    assert lambda_index._pod_uses_efa(8, "h100", is_multinode=False) is False


def test_pod_uses_efa_t4_small_excluded(lambda_index):
    assert lambda_index._pod_uses_efa(1, "t4-small", is_multinode=True) is False


# ── get_available_gpus_on_node: MIG-slice aggregation ─────────────────────────

def test_available_gpus_subtracts_used_mig_slices(lambda_index):
    node = _node("n1", allocatable={"nvidia.com/mig-1g.10gb": "16"})
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.return_value = MagicMock(
        items=[_pod_with_request("nvidia.com/mig-1g.10gb", 2)])
    assert lambda_index.get_available_gpus_on_node(v1, node, "h100-mig-1g") == 14


def test_available_gpus_counts_pending_pods_as_used(lambda_index):
    node = _node("n1", allocatable={"nvidia.com/gpu": "8"})
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.return_value = MagicMock(items=[
        _pod_with_request("nvidia.com/gpu", 2, phase="Running"),
        _pod_with_request("nvidia.com/gpu", 3, phase="Pending"),
    ])
    assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 3


def test_available_gpus_ignores_terminated_pods(lambda_index):
    node = _node("n1", allocatable={"nvidia.com/gpu": "8"})
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.return_value = MagicMock(items=[
        _pod_with_request("nvidia.com/gpu", 4, phase="Succeeded"),
    ])
    assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 8


def test_available_gpus_zero_allocatable_short_circuits(lambda_index):
    node = _node("n1", allocatable={})
    v1 = MagicMock()
    assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0
    # short-circuited before listing pods
    v1.list_pod_for_all_namespaces.assert_not_called()


def test_available_gpus_never_negative(lambda_index):
    node = _node("n1", allocatable={"nvidia.com/gpu": "2"})
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.return_value = MagicMock(
        items=[_pod_with_request("nvidia.com/gpu", 5)])
    assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0


def test_available_gpus_error_returns_zero(lambda_index):
    node = _node("n1", allocatable={"nvidia.com/gpu": "8"})
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.side_effect = RuntimeError("api down")
    assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0


def test_available_gpus_default_resource_when_no_type(lambda_index):
    # gpu_type=None -> nvidia.com/gpu
    node = _node("n1", allocatable={"nvidia.com/gpu": "4"})
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.return_value = MagicMock(items=[])
    assert lambda_index.get_available_gpus_on_node(v1, node, None) == 4


# ── get_instance_type_and_gpu_info: MIG SKU resolution from request ───────────

def test_instance_info_mig_resolves_sku(lambda_index):
    v1 = MagicMock()
    pod = MagicMock()
    pod.spec.node_name = "node-a"
    pod.spec.containers = [_pod_with_request("nvidia.com/mig-2g.20gb", 1).spec.containers[0]]
    v1.read_namespaced_pod.return_value = pod
    node = MagicMock()
    node.metadata.labels = {"node.kubernetes.io/instance-type": "p5.48xlarge"}
    v1.read_node.return_value = node
    with patch.object(lambda_index.client, "CoreV1Api", return_value=v1):
        itype, gtype = lambda_index.get_instance_type_and_gpu_info(MagicMock(), "p1")
    assert itype == "p5.48xlarge"
    assert gtype == "h100-mig-2g"


def test_instance_info_mig_memory_doubled_variant(lambda_index):
    # nvidia.com/mig-1g.20gb maps to the same h100-mig-1g SKU bucket
    v1 = MagicMock()
    pod = MagicMock()
    pod.spec.node_name = "node-a"
    pod.spec.containers = [_pod_with_request("nvidia.com/mig-1g.20gb", 1).spec.containers[0]]
    v1.read_namespaced_pod.return_value = pod
    node = MagicMock()
    node.metadata.labels = {"node.kubernetes.io/instance-type": "p5.48xlarge"}
    v1.read_node.return_value = node
    with patch.object(lambda_index.client, "CoreV1Api", return_value=v1):
        _, gtype = lambda_index.get_instance_type_and_gpu_info(MagicMock(), "p1")
    assert gtype == "h100-mig-1g"


def test_instance_info_full_gpu_maps_instance_type(lambda_index):
    v1 = MagicMock()
    pod = MagicMock()
    pod.spec.node_name = "node-b"
    pod.spec.containers = [_pod_with_request("nvidia.com/gpu", 8).spec.containers[0]]
    v1.read_namespaced_pod.return_value = pod
    node = MagicMock()
    node.metadata.labels = {"node.kubernetes.io/instance-type": "p6-b200.48xlarge"}
    v1.read_node.return_value = node
    with patch.object(lambda_index.client, "CoreV1Api", return_value=v1):
        itype, gtype = lambda_index.get_instance_type_and_gpu_info(MagicMock(), "p2")
    assert itype == "p6-b200.48xlarge"
    assert gtype == "B200"


def test_instance_info_unmapped_instance_is_unknown(lambda_index):
    v1 = MagicMock()
    pod = MagicMock()
    pod.spec.node_name = "node-c"
    pod.spec.containers = [_pod_with_request("nvidia.com/gpu", 1).spec.containers[0]]
    v1.read_namespaced_pod.return_value = pod
    node = MagicMock()
    node.metadata.labels = {"node.kubernetes.io/instance-type": "some.weird.type"}
    v1.read_node.return_value = node
    with patch.object(lambda_index.client, "CoreV1Api", return_value=v1):
        itype, gtype = lambda_index.get_instance_type_and_gpu_info(MagicMock(), "p3")
    assert itype == "some.weird.type"
    assert gtype == "Unknown"


def test_instance_info_no_node_returns_unknown(lambda_index):
    v1 = MagicMock()
    pod = MagicMock()
    pod.spec.node_name = None
    v1.read_namespaced_pod.return_value = pod
    with patch.object(lambda_index.client, "CoreV1Api", return_value=v1):
        assert lambda_index.get_instance_type_and_gpu_info(MagicMock(), "p4") == (
            "unknown", "unknown")


# ── get_target_az_for_reservation: binpacking + fallbacks ─────────────────────

def _patch_az(idx, v1, avail):
    """Patch the cluster-touching deps of get_target_az_for_reservation.
    `avail` is a dict node_name -> available_gpus.
    """
    return (
        patch.object(idx, "get_k8s_client", return_value=MagicMock()),
        patch.object(idx.client, "CoreV1Api", return_value=v1),
        patch.object(idx, "get_available_gpus_on_node",
                     side_effect=lambda v1api, node, gt: avail[node.metadata.name]),
    )


def test_target_az_binpacks_fullest_fitting_node(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[
        _node("a", "us-east-2a"), _node("b", "us-east-2b")])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 2, "b": 4})
    with p1, p2, p3:
        # request 2: both fit, pick the fullest (fewest free => 'a' with 2)
        assert idx.get_target_az_for_reservation("h100", 2) == ("us-east-2a", "a")


def test_target_az_selects_by_node_gpu_label(lambda_index):
    # MIG SKU must select nodes by the physical GpuType label (h100), not the SKU
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[_node("a", "us-east-2a")])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 16})
    with p1, p2, p3:
        idx.get_target_az_for_reservation("h100-mig-1g", 1)
    sel = v1.list_node.call_args.kwargs["label_selector"]
    assert sel == "GpuType=h100"


def test_target_az_no_fit_returns_best_az_no_node(lambda_index):
    # t4 is NOT in WARM_POOL_TARGETS -> no eviction; no node has 8 -> best-AZ, None
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[
        _node("a", "us-east-2a"), _node("b", "us-east-2b")])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 1, "b": 3})
    with p1, p2, p3:
        az, node = idx.get_target_az_for_reservation("t4", 8)
    assert az == "us-east-2b"   # node with most free GPUs
    assert node is None


def test_target_az_no_ready_nodes_returns_none_none(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[_node("a", "us-east-2a", ready=False)])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 0})
    with p1, p2, p3:
        assert idx.get_target_az_for_reservation("t4", 1) == (None, None)


def test_target_az_unschedulable_node_skipped(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[
        _node("a", "us-east-2a", schedulable=False)])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 8})
    with p1, p2, p3:
        # the only node is cordoned -> treated as no ready/schedulable nodes
        assert idx.get_target_az_for_reservation("t4", 1) == (None, None)


def test_target_az_node_without_zone_label_skipped(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[_node("a", az=None)])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 8})
    with p1, p2, p3:
        # no AZ label -> node skipped -> no candidates / ready nodes
        assert idx.get_target_az_for_reservation("t4", 1) == (None, None)


def test_target_az_exception_returns_primary_az(lambda_index):
    idx = lambda_index
    with patch.object(idx, "get_k8s_client", side_effect=RuntimeError("boom")):
        az, node = idx.get_target_az_for_reservation("h100", 1)
    assert az == idx.PRIMARY_AVAILABILITY_ZONE
    assert node is None


def test_target_az_warm_eviction_when_no_fit(lambda_index):
    # h100 IS in WARM_POOL_TARGETS -> when nothing fits, eviction runs and can
    # reclaim a node.
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[_node("a", "us-east-2a")])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 0})
    with p1, p2, p3, patch.object(
            idx, "_evict_warm_for_capacity",
            return_value=("us-east-2a", "a")) as ev:
        az, node = idx.get_target_az_for_reservation("h100", 8)
    assert (az, node) == ("us-east-2a", "a")
    ev.assert_called_once()
    # eviction called with the requested gpu_type and count
    assert ev.call_args.args[1] == "h100"
    assert ev.call_args.args[2] == 8


def test_target_az_eviction_miss_falls_back_to_best_az(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    v1.list_node.return_value = MagicMock(items=[
        _node("a", "us-east-2a"), _node("b", "us-east-2b")])
    p1, p2, p3 = _patch_az(idx, v1, {"a": 1, "b": 2})
    with p1, p2, p3, patch.object(
            idx, "_evict_warm_for_capacity", return_value=(None, None)):
        az, node = idx.get_target_az_for_reservation("h100", 8)
    assert az == "us-east-2b"
    assert node is None


# ── _evict_warm_for_capacity ──────────────────────────────────────────────────

def test_evict_frees_minimum_pods_on_single_node(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    pods = [_warm_pod("w1", "nodeA"), _warm_pod("w2", "nodeA")]
    ready = [{"node_name": "nodeA", "az": "us-east-2a", "available_gpus": 6}]
    with patch.object(idx, "_list_warm_pods", return_value=pods):
        az, node = idx._evict_warm_for_capacity(v1, "h100", 8, ready)
    assert (az, node) == ("us-east-2a", "nodeA")
    assert v1.delete_namespaced_pod.call_count == 2


def test_evict_stops_once_request_satisfied(lambda_index):
    # node already has 7 free, request 8 -> only need to evict ONE 1-GPU warm pod
    idx = lambda_index
    v1 = MagicMock()
    pods = [_warm_pod("w1", "nodeA"), _warm_pod("w2", "nodeA")]
    ready = [{"node_name": "nodeA", "az": "us-east-2a", "available_gpus": 7}]
    with patch.object(idx, "_list_warm_pods", return_value=pods):
        az, node = idx._evict_warm_for_capacity(v1, "h100", 8, ready)
    assert (az, node) == ("us-east-2a", "nodeA")
    assert v1.delete_namespaced_pod.call_count == 1


def test_evict_returns_none_when_cannot_satisfy(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    pods = [_warm_pod("w1", "nodeA")]  # only 1 GPU available to free
    ready = [{"node_name": "nodeA", "az": "us-east-2a", "available_gpus": 0}]
    with patch.object(idx, "_list_warm_pods", return_value=pods):
        assert idx._evict_warm_for_capacity(v1, "h100", 8, ready) == (None, None)
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_skips_pending_warm_pods(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    pods = [_warm_pod("w1", "nodeA", phase="Pending")]
    ready = [{"node_name": "nodeA", "az": "us-east-2a", "available_gpus": 0}]
    with patch.object(idx, "_list_warm_pods", return_value=pods):
        # the only candidate isn't Running -> nothing to free -> None
        assert idx._evict_warm_for_capacity(v1, "h100", 1, ready) == (None, None)


def test_evict_deletes_ssh_service_too(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    pods = [_warm_pod("w1", "nodeA")]
    ready = [{"node_name": "nodeA", "az": "us-east-2a", "available_gpus": 0}]
    with patch.object(idx, "_list_warm_pods", return_value=pods):
        idx._evict_warm_for_capacity(v1, "h100", 1, ready)
    v1.delete_namespaced_pod.assert_called_once_with("w1", "gpu-dev")
    v1.delete_namespaced_service.assert_called_once_with("w1-ssh", "gpu-dev")


def test_evict_label_guard_blocks_non_idle_delete(lambda_index):
    # Hard guard: re-checks labels right before delete; a pod whose label is no
    # longer 'app=gpu-dev-warm,warm-state=ready' must NOT be deleted.
    idx = lambda_index
    v1 = MagicMock()
    bad = _warm_pod("w1", "nodeA",
                    labels={"app": "gpu-dev-warm", "warm-state": "provisioning"})
    ready = [{"node_name": "nodeA", "az": "us-east-2a", "available_gpus": 0}]
    with patch.object(idx, "_list_warm_pods", return_value=[bad]):
        idx._evict_warm_for_capacity(v1, "h100", 1, ready)
    # the guard prevented the actual delete even though the pod was selected
    v1.delete_namespaced_pod.assert_not_called()


def test_evict_swallows_errors_returns_none(lambda_index):
    idx = lambda_index
    v1 = MagicMock()
    with patch.object(idx, "_list_warm_pods", side_effect=RuntimeError("list down")):
        assert idx._evict_warm_for_capacity(v1, "h100", 1, []) == (None, None)
