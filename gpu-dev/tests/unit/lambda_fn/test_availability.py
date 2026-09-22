"""Unit tests for the availability math in the reservation_processor lambda.

Covers:
- get_gpu_resource_name / get_node_gpu_type / _warm_gpu_count (config helpers)
- get_available_gpus_on_node (allocatable - used; phase filter; resource filter;
  Decimal/string coercion; max(0, ...) clamp; exception -> 0)
- get_target_az_for_reservation (candidate selection, binpacking, fallback AZ,
  warm-eviction branch, no-nodes branch, exception -> primary AZ)
- _evict_warm_for_capacity (min-eviction math, single-node, label re-check guard)

All k8s access is mocked: nodes/pods are tiny SimpleNamespace stand-ins built to
match the attributes the source reads (node.status.allocatable, node.metadata...,
pod.status.phase, pod.spec.containers[].resources.requests, etc.).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock


# --------------------------------------------------------------------------- #
# Tiny k8s object factories (only the fields the source actually reads)
# --------------------------------------------------------------------------- #
def make_node(name, allocatable, az="us-east-2a", ready=True,
              schedulable=True, gpu_type_label=None, zone_label=True):
    labels = {}
    if zone_label and az is not None:
        labels["topology.kubernetes.io/zone"] = az
    if gpu_type_label is not None:
        labels["GpuType"] = gpu_type_label
    conditions = [SimpleNamespace(type="Ready", status="True" if ready else "False")]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        status=SimpleNamespace(allocatable=allocatable, conditions=conditions),
        spec=SimpleNamespace(unschedulable=(not schedulable)),
    )


def make_pod(phase="Running", requests=None, name="p", node_name="n1",
             warm_state=None, app=None):
    """A pod that requests `requests` (dict resource->str) in one container."""
    container = SimpleNamespace(
        resources=SimpleNamespace(requests=dict(requests) if requests else None)
    )
    labels = {}
    if app is not None:
        labels["app"] = app
    if warm_state is not None:
        labels["warm-state"] = warm_state
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels),
        status=SimpleNamespace(phase=phase),
        spec=SimpleNamespace(containers=[container], node_name=node_name),
    )


def make_pod_no_container(phase="Running", node_name="n1"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="empty", labels={}),
        status=SimpleNamespace(phase=phase),
        spec=SimpleNamespace(containers=None, node_name=node_name),
    )


def pod_list(pods):
    return SimpleNamespace(items=list(pods))


# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #
class TestResourceHelpers:
    def test_full_gpu_resource_name_default(self, lambda_index):
        assert lambda_index.get_gpu_resource_name("h100") == "nvidia.com/gpu"
        assert lambda_index.get_gpu_resource_name("b200") == "nvidia.com/gpu"

    def test_mig_resource_name(self, lambda_index):
        assert lambda_index.get_gpu_resource_name("h100-mig-1g") == "nvidia.com/mig-1g.10gb"
        assert lambda_index.get_gpu_resource_name("b200-mig-2g") == "nvidia.com/mig-2g.45gb"

    def test_unknown_type_falls_back_to_default(self, lambda_index):
        # GPU_CONFIG_DEFAULT has no k8s_resource key -> generic nvidia.com/gpu
        assert lambda_index.get_gpu_resource_name("does-not-exist") == "nvidia.com/gpu"

    def test_node_gpu_type_mig_maps_to_physical(self, lambda_index):
        assert lambda_index.get_node_gpu_type("h100-mig-3g") == "h100"
        assert lambda_index.get_node_gpu_type("b200-mig-1g") == "b200"

    def test_node_gpu_type_full_gpu_identity(self, lambda_index):
        # full GPUs have no node_gpu_type override -> returns the type itself
        assert lambda_index.get_node_gpu_type("h100") == "h100"

    def test_node_gpu_type_unknown_returns_input(self, lambda_index):
        assert lambda_index.get_node_gpu_type("mystery") == "mystery"

    def test_warm_gpu_count_cpu_is_zero(self, lambda_index):
        assert lambda_index._warm_gpu_count("cpu-arm") == 0
        assert lambda_index._warm_gpu_count("cpu-x86") == 0
        assert lambda_index._warm_gpu_count("cpu-spot") == 0

    def test_warm_gpu_count_gpu_is_one(self, lambda_index):
        assert lambda_index._warm_gpu_count("h100") == 1
        assert lambda_index._warm_gpu_count("b200") == 1
        assert lambda_index._warm_gpu_count("h100-mig-1g") == 1


# --------------------------------------------------------------------------- #
# get_available_gpus_on_node
# --------------------------------------------------------------------------- #
class TestGetAvailableGpusOnNode:
    def test_no_allocatable_gpus_returns_zero_without_listing_pods(self, lambda_index):
        v1 = MagicMock()
        node = make_node("n1", allocatable={"nvidia.com/gpu": "0"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0
        # short-circuits before querying pods
        v1.list_pod_for_all_namespaces.assert_not_called()

    def test_missing_resource_key_treated_as_zero(self, lambda_index):
        v1 = MagicMock()
        node = make_node("n1", allocatable={"cpu": "192"})  # no gpu key at all
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0
        v1.list_pod_for_all_namespaces.assert_not_called()

    def test_none_allocatable_treated_as_empty(self, lambda_index):
        v1 = MagicMock()
        node = make_node("n1", allocatable=None)
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0

    def test_full_node_no_pods_returns_all(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 8

    def test_subtracts_running_pod_requests(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Running", requests={"nvidia.com/gpu": "2"}),
            make_pod(phase="Running", requests={"nvidia.com/gpu": "1"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 5

    def test_pending_pods_also_count_as_used(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Pending", requests={"nvidia.com/gpu": "4"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 4

    def test_terminated_pods_do_not_count(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Succeeded", requests={"nvidia.com/gpu": "8"}),
            make_pod(phase="Failed", requests={"nvidia.com/gpu": "8"}),
            make_pod(phase="Running", requests={"nvidia.com/gpu": "1"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 7

    def test_only_matching_resource_counted(self, lambda_index):
        # A MIG slice pod must not subtract from full-GPU availability and vice versa.
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Running", requests={"nvidia.com/mig-1g.10gb": "3"}),
            make_pod(phase="Running", requests={"nvidia.com/gpu": "2"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        # only the nvidia.com/gpu request (2) is counted
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 6

    def test_mig_resource_counting(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Running", requests={"nvidia.com/mig-1g.10gb": "5"}),
            make_pod(phase="Running", requests={"nvidia.com/gpu": "8"}),  # ignored
        ])
        node = make_node("n1", allocatable={"nvidia.com/mig-1g.10gb": "16"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100-mig-1g") == 11

    def test_overcommit_clamped_to_zero(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Running", requests={"nvidia.com/gpu": "8"}),
            make_pod(phase="Pending", requests={"nvidia.com/gpu": "4"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        # 8 - 12 = -4 -> clamp to 0, never negative
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0

    def test_pod_without_containers_skipped(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod_no_container(phase="Running"),
            make_pod(phase="Running", requests={"nvidia.com/gpu": "1"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 7

    def test_pod_with_no_requests_skipped(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Running", requests=None),  # resources.requests is None
            make_pod(phase="Running", requests={"nvidia.com/gpu": "3"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 5

    def test_default_resource_when_gpu_type_none(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([
            make_pod(phase="Running", requests={"nvidia.com/gpu": "1"}),
        ])
        node = make_node("n1", allocatable={"nvidia.com/gpu": "4"})
        # gpu_type=None -> resource_name defaults to nvidia.com/gpu
        assert lambda_index.get_available_gpus_on_node(v1, node) == 3

    def test_uses_node_name_field_selector(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.return_value = pod_list([])
        node = make_node("the-node", allocatable={"nvidia.com/gpu": "8"})
        lambda_index.get_available_gpus_on_node(v1, node, "h100")
        _, kwargs = v1.list_pod_for_all_namespaces.call_args
        assert kwargs["field_selector"] == "spec.nodeName=the-node"

    def test_exception_returns_zero(self, lambda_index):
        v1 = MagicMock()
        v1.list_pod_for_all_namespaces.side_effect = RuntimeError("k8s api down")
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"})
        assert lambda_index.get_available_gpus_on_node(v1, node, "h100") == 0


# --------------------------------------------------------------------------- #
# get_target_az_for_reservation
# --------------------------------------------------------------------------- #
class TestGetTargetAzForReservation:
    def _patch_k8s(self, monkeypatch, lambda_index, nodes):
        """Wire get_k8s_client + client.CoreV1Api(...).list_node to return nodes."""
        v1 = MagicMock()
        v1.list_node.return_value = pod_list(nodes)
        monkeypatch.setattr(lambda_index, "get_k8s_client", lambda: object())
        monkeypatch.setattr(lambda_index.client, "CoreV1Api", lambda _c: v1)
        return v1

    def test_picks_az_of_fitting_node(self, monkeypatch, lambda_index):
        node = make_node("n1", allocatable={"nvidia.com/gpu": "8"},
                         az="us-east-2b", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [node])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 8)
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 4)
        assert az == "us-east-2b"
        assert target_node == "n1"

    def test_binpacks_into_fullest_fitting_node(self, monkeypatch, lambda_index):
        # n_loose has 8 free, n_tight has 4 free; request 2 -> pick the tighter one.
        n_loose = make_node("n-loose", allocatable={"nvidia.com/gpu": "8"},
                            az="us-east-2a", gpu_type_label="h100")
        n_tight = make_node("n-tight", allocatable={"nvidia.com/gpu": "8"},
                            az="us-east-2c", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [n_loose, n_tight])
        free = {"n-loose": 8, "n-tight": 4}
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: free[n.metadata.name])
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 2)
        assert target_node == "n-tight"
        assert az == "us-east-2c"

    def test_binpack_tie_broken_by_node_name(self, monkeypatch, lambda_index):
        n_b = make_node("node-b", allocatable={"nvidia.com/gpu": "8"},
                        az="az-b", gpu_type_label="h100")
        n_a = make_node("node-a", allocatable={"nvidia.com/gpu": "8"},
                        az="az-a", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [n_b, n_a])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 4)  # equal free -> name tiebreak
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 2)
        assert target_node == "node-a"
        assert az == "az-a"

    def test_skips_not_ready_nodes(self, monkeypatch, lambda_index):
        bad = make_node("bad", allocatable={"nvidia.com/gpu": "8"},
                        az="az-bad", ready=False, gpu_type_label="h100")
        good = make_node("good", allocatable={"nvidia.com/gpu": "8"},
                         az="az-good", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [bad, good])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 8)
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 4)
        assert target_node == "good"
        assert az == "az-good"

    def test_skips_unschedulable_nodes(self, monkeypatch, lambda_index):
        cordoned = make_node("cordoned", allocatable={"nvidia.com/gpu": "8"},
                             az="az-x", schedulable=False, gpu_type_label="h100")
        good = make_node("good", allocatable={"nvidia.com/gpu": "8"},
                         az="az-good", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [cordoned, good])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 8)
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 4)
        assert target_node == "good"

    def test_node_without_az_label_skipped(self, monkeypatch, lambda_index):
        no_az = make_node("no-az", allocatable={"nvidia.com/gpu": "8"},
                          az=None, zone_label=False, gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [no_az])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 8)
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 1)
        # node skipped entirely -> no ready nodes -> (None, None)
        assert az is None
        assert target_node is None

    def test_fallback_az_when_no_node_fits_and_not_warm(self, monkeypatch, lambda_index):
        # Request bigger than any single node free; gpu_type NOT in WARM_POOL_TARGETS
        # so no eviction. Returns AZ of most-free node with no node hint.
        n1 = make_node("n1", allocatable={"nvidia.com/gpu": "8"},
                       az="az-1", gpu_type_label="a100")
        n2 = make_node("n2", allocatable={"nvidia.com/gpu": "8"},
                       az="az-2", gpu_type_label="a100")
        self._patch_k8s(monkeypatch, lambda_index, [n1, n2])
        free = {"n1": 1, "n2": 3}
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: free[n.metadata.name])
        assert "a100" not in lambda_index.WARM_POOL_TARGETS
        az, target_node = lambda_index.get_target_az_for_reservation("a100", 8)
        assert az == "az-2"      # n2 is most-free
        assert target_node is None

    def test_warm_eviction_branch_when_no_node_fits(self, monkeypatch, lambda_index):
        # h100 IS in WARM_POOL_TARGETS -> eviction is attempted. Stub it to succeed.
        n1 = make_node("n1", allocatable={"nvidia.com/gpu": "8"},
                       az="az-1", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [n1])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 1)  # not enough for an 8-GPU req
        called = {}

        def fake_evict(v1, gpu_type, gpus_requested, ready_nodes):
            called["args"] = (gpu_type, gpus_requested, [n["node_name"] for n in ready_nodes])
            return "az-evicted", "n1"

        monkeypatch.setattr(lambda_index, "_evict_warm_for_capacity", fake_evict)
        assert "h100" in lambda_index.WARM_POOL_TARGETS
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 8)
        assert (az, target_node) == ("az-evicted", "n1")
        assert called["args"][0] == "h100"
        assert called["args"][1] == 8

    def test_warm_eviction_miss_falls_back_to_az(self, monkeypatch, lambda_index):
        n1 = make_node("n1", allocatable={"nvidia.com/gpu": "8"},
                       az="az-most", gpu_type_label="h100")
        self._patch_k8s(monkeypatch, lambda_index, [n1])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 2)
        monkeypatch.setattr(lambda_index, "_evict_warm_for_capacity",
                            lambda *a, **k: (None, None))
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 8)
        assert az == "az-most"
        assert target_node is None

    def test_no_nodes_at_all_returns_none_none(self, monkeypatch, lambda_index):
        self._patch_k8s(monkeypatch, lambda_index, [])
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 1)
        assert az is None
        assert target_node is None

    def test_exception_returns_primary_az(self, monkeypatch, lambda_index):
        def boom():
            raise RuntimeError("no cluster")
        monkeypatch.setattr(lambda_index, "get_k8s_client", boom)
        az, target_node = lambda_index.get_target_az_for_reservation("h100", 1)
        assert az == lambda_index.PRIMARY_AVAILABILITY_ZONE
        assert target_node is None

    def test_list_node_uses_physical_label_for_mig(self, monkeypatch, lambda_index):
        v1 = self._patch_k8s(monkeypatch, lambda_index, [])
        monkeypatch.setattr(lambda_index, "get_available_gpus_on_node",
                            lambda v1, n, t: 0)
        lambda_index.get_target_az_for_reservation("h100-mig-1g", 1)
        _, kwargs = v1.list_node.call_args
        # MIG SKU selects the underlying physical node label (h100)
        assert kwargs["label_selector"] == "GpuType=h100"


# --------------------------------------------------------------------------- #
# _evict_warm_for_capacity (the warm-ready math)
# --------------------------------------------------------------------------- #
class TestEvictWarmForCapacity:
    def _warm_pod(self, name, node, held=1, phase="Running",
                  warm_state="ready", app="gpu-dev-warm"):
        return make_pod(
            phase=phase,
            requests={"nvidia.com/gpu": str(held)} if held else None,
            name=name, node_name=node, warm_state=warm_state, app=app,
        )

    def test_evicts_minimum_pods_to_satisfy_request(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        # one node with 0 free + 4 warm pods (1 GPU each). Request 2 -> evict exactly 2.
        warm = [self._warm_pod(f"w{i}", "node-1") for i in range(4)]
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 2, ready_nodes)
        assert (az, node) == ("az-1", "node-1")
        assert v1.delete_namespaced_pod.call_count == 2  # minimum, not all 4

    def test_existing_free_reduces_evictions(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        warm = [self._warm_pod(f"w{i}", "node-1") for i in range(4)]
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        # node already has 1 free, request 2 -> only need to free 1 more -> 1 evict
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 1}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 2, ready_nodes)
        assert (az, node) == ("az-1", "node-1")
        assert v1.delete_namespaced_pod.call_count == 1

    def test_returns_none_when_eviction_insufficient(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        # only 1 warm pod on the node but request needs 4 -> can't satisfy -> no delete
        warm = [self._warm_pod("w0", "node-1")]
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 4, ready_nodes)
        assert (az, node) == (None, None)
        v1.delete_namespaced_pod.assert_not_called()

    def test_single_node_only_no_cross_node_eviction(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        # 1 warm on each of two nodes; request 2 -> neither node alone satisfies -> none
        warm = [self._warm_pod("w-a", "node-a"), self._warm_pod("w-b", "node-b")]
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        ready_nodes = [
            {"node_name": "node-a", "az": "az-a", "available_gpus": 0},
            {"node_name": "node-b", "az": "az-b", "available_gpus": 0},
        ]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 2, ready_nodes)
        assert (az, node) == (None, None)
        v1.delete_namespaced_pod.assert_not_called()

    def test_non_running_warm_pod_ignored(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        warm = [self._warm_pod("w0", "node-1", phase="Pending")]
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 1, ready_nodes)
        assert (az, node) == (None, None)
        v1.delete_namespaced_pod.assert_not_called()

    def test_warm_pod_without_node_ignored(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        warm = [self._warm_pod("w0", None)]  # not scheduled to a node
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 1, ready_nodes)
        assert (az, node) == (None, None)

    def test_label_guard_skips_non_idle_pod_before_delete(self, monkeypatch, lambda_index):
        # _list_warm_pods returns a pod whose label is NOT ready (race-guard path);
        # the function must skip deleting it. Here it's the only candidate so the
        # request stays unsatisfiable and no delete happens.
        v1 = MagicMock()
        racey = self._warm_pod("w0", "node-1", warm_state="claimed")
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: [racey])
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 1, ready_nodes)
        # the pod counts toward the freed math (it holds a GPU + is Running), so the
        # branch is entered, but the per-pod guard refuses to delete a non-ready pod.
        v1.delete_namespaced_pod.assert_not_called()
        # az/node still returned because the freed-arithmetic was satisfied
        assert (az, node) == ("az-1", "node-1")

    def test_also_deletes_ssh_service(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        warm = [self._warm_pod("w0", "node-1")]
        monkeypatch.setattr(lambda_index, "_list_warm_pods", lambda *a, **k: warm)
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        lambda_index._evict_warm_for_capacity(v1, "h100", 1, ready_nodes)
        v1.delete_namespaced_pod.assert_called_once_with("w0", "gpu-dev")
        v1.delete_namespaced_service.assert_called_once_with("w0-ssh", "gpu-dev")

    def test_exception_returns_none_none(self, monkeypatch, lambda_index):
        v1 = MagicMock()
        monkeypatch.setattr(lambda_index, "_list_warm_pods",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        ready_nodes = [{"node_name": "node-1", "az": "az-1", "available_gpus": 0}]
        az, node = lambda_index._evict_warm_for_capacity(v1, "h100", 1, ready_nodes)
        assert (az, node) == (None, None)
