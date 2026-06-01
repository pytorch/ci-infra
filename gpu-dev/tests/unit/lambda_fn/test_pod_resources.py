"""Unit tests for reservation_processor pod resource sizing.

Covers ``index.get_pod_resource_limits`` / ``get_pod_resource_requests``:
  * proportional CPU/memory scaling per gpu_count for full-GPU SKUs
  * MIG slice-fraction sizing (1g/2g/3g)
  * cpu-* fixed sizing and absence of the GPU/EFA branches
  * Decimal -> int coercion (the `decimal * float` bug fix)
  * EFA gating (multinode + full node only; never t4-small / MIG / cpu-*)
  * gpu_count == 0 short-circuit and unknown-SKU default config

All expected numbers were derived from the source formulas and cross-checked
against the live functions; they encode REAL behavior, not the implementation
restated.
"""
from decimal import Decimal

import pytest

# ``index`` is importable via the root conftest (sys.path + env already set).
import index


# --------------------------------------------------------------------------- #
# Full-GPU limits: proportional CPU (1.5x, capped at cpus) + memory (gpu_ratio)
# --------------------------------------------------------------------------- #
class TestFullGpuLimits:
    def test_h100_single_gpu_limits(self):
        # ratio 1/8: cpu = min(192, int(192*0.125*1.5)=36)=36, mem = int(2048*0.125)=256
        lim = index.get_pod_resource_limits(1, "h100")
        assert lim == {"nvidia.com/gpu": "1", "cpu": "36", "memory": "256Gi"}

    def test_h100_full_node_limits_cpu_capped(self):
        # ratio 1.0: fractional_cpu*1.5 = 288 but capped at config cpus (192)
        lim = index.get_pod_resource_limits(8, "h100")
        assert lim == {"nvidia.com/gpu": "8", "cpu": "192", "memory": "2048Gi"}
        # The 1.5x boost must NOT exceed the node's physical cpu count.
        assert int(lim["cpu"]) == 192

    def test_t4_two_gpu_limits_use_default_resource_name(self):
        # t4: cpus=48, mem=192, max_gpus=4. ratio 0.5: cpu=min(48,int(48*0.5*1.5)=36)=36, mem=96
        lim = index.get_pod_resource_limits(2, "t4")
        assert lim == {"nvidia.com/gpu": "2", "cpu": "36", "memory": "96Gi"}

    def test_values_are_strings(self):
        lim = index.get_pod_resource_limits(4, "h100")
        assert all(isinstance(v, str) for v in lim.values())
        # memory always carries the Gi suffix; cpu never does.
        assert lim["memory"].endswith("Gi")
        assert "Gi" not in lim["cpu"]


# --------------------------------------------------------------------------- #
# Full-GPU requests: same ratio scaled by 0.9 (slightly below limits)
# --------------------------------------------------------------------------- #
class TestFullGpuRequests:
    def test_h100_single_gpu_requests(self):
        # cpu = int(192*0.125*0.9)=int(21.6)=21, mem = int(2048*0.125*0.9)=int(230.4)=230
        req = index.get_pod_resource_requests(1, "h100")
        assert req == {"nvidia.com/gpu": "1", "cpu": "21", "memory": "230Gi"}

    def test_h100_full_node_requests(self):
        # ratio 1.0: cpu=int(192*0.9)=172, mem=int(2048*0.9)=1843
        req = index.get_pod_resource_requests(8, "h100")
        assert req == {"nvidia.com/gpu": "8", "cpu": "172", "memory": "1843Gi"}

    def test_requests_below_limits(self):
        # requests (0.9x) must be <= limits for the same allocation.
        for n in (1, 2, 4, 8):
            req = index.get_pod_resource_requests(n, "h100")
            lim = index.get_pod_resource_limits(n, "h100")
            assert int(req["cpu"]) <= int(lim["cpu"])
            assert int(req["memory"][:-2]) <= int(lim["memory"][:-2])


# --------------------------------------------------------------------------- #
# MIG: sized by slice fraction (slices/7) * (host cpu / 8 GPUs)
# --------------------------------------------------------------------------- #
class TestMigSizing:
    def test_mig_1g_limits(self):
        # 1g => 1/7. cpu_per_full=192/8=24, mem_per_full=2048/8=256
        # cpu = max(1, min(192, int(24*(1/7)*1*1.5)=5))=5; mem = max(1, int(256*(1/7)))=36
        lim = index.get_pod_resource_limits(1, "h100-mig-1g")
        assert lim == {"nvidia.com/mig-1g.10gb": "1", "cpu": "5", "memory": "36Gi"}

    def test_mig_1g_requests(self):
        # requests apply the extra 0.9 factor: cpu=int(24*(1/7)*0.9)=3, mem=int(256*(1/7)*0.9)=32
        req = index.get_pod_resource_requests(1, "h100-mig-1g")
        assert req == {"nvidia.com/mig-1g.10gb": "1", "cpu": "3", "memory": "32Gi"}

    def test_mig_3g_two_slices_limits(self):
        # 3g => 3/7, count 2. cpu = max(1, min(192, int(24*(3/7)*2*1.5)=30))=30
        # mem = max(1, int(256*(3/7)*2)=219)=219
        lim = index.get_pod_resource_limits(2, "h100-mig-3g")
        assert lim == {"nvidia.com/mig-3g.40gb": "2", "cpu": "30", "memory": "219Gi"}

    def test_mig_uses_custom_k8s_resource_not_nvidia_gpu(self):
        lim = index.get_pod_resource_limits(1, "b200-mig-2g")
        assert "nvidia.com/mig-2g.45gb" in lim
        assert "nvidia.com/gpu" not in lim

    def test_mig_slice_fraction_helper(self):
        assert index._mig_slice_fraction("h100-mig-1g") == pytest.approx(1 / 7)
        assert index._mig_slice_fraction("h100-mig-3g") == pytest.approx(3 / 7)
        # non-MIG types are a whole GPU.
        assert index._mig_slice_fraction("h100") == 1.0
        # malformed mig suffix falls back to 1.0 (no crash).
        assert index._mig_slice_fraction("h100-mig-xg") == 1.0

    def test_mig_floor_at_one_cpu_and_mem(self):
        # Smallest slice still yields >=1 cpu and >=1Gi memory (max(1, ...)).
        lim = index.get_pod_resource_limits(1, "b200-mig-1g")
        assert int(lim["cpu"]) >= 1
        assert int(lim["memory"][:-2]) >= 1


# --------------------------------------------------------------------------- #
# CPU-only SKUs: fixed limits/requests, no GPU resource, no EFA
# --------------------------------------------------------------------------- #
class TestCpuOnly:
    def test_cpu_arm_limits_reserve_two_cpu_and_two_gib(self):
        # cpu = cpus-2 = 30, memory = (memory_gb-2)Gi = 62Gi
        lim = index.get_pod_resource_limits(0, "cpu-arm")
        assert lim == {"cpu": "30", "memory": "62Gi"}

    def test_cpu_arm_requests_fixed(self):
        req = index.get_pod_resource_requests(0, "cpu-arm")
        assert req == {"cpu": "2", "memory": "4Gi"}

    def test_cpu_spot_smaller_node(self):
        # cpu-spot: cpus=8, memory_gb=16
        lim = index.get_pod_resource_limits(0, "cpu-spot")
        assert lim == {"cpu": "6", "memory": "14Gi"}

    def test_cpu_has_no_gpu_resource(self):
        lim = index.get_pod_resource_limits(0, "cpu-x86")
        assert not any(k.startswith("nvidia.com/") for k in lim)

    def test_cpu_gpu_count_ignored(self):
        # gpu_count is irrelevant for cpu-* (branch keyed on the "cpu-" prefix).
        assert index.get_pod_resource_limits(0, "cpu-arm") == index.get_pod_resource_limits(4, "cpu-arm")

    def test_cpu_never_gets_efa_even_multinode(self):
        lim = index.get_pod_resource_limits(0, "cpu-arm", is_multinode=True)
        assert "vpc.amazonaws.com/efa" not in lim
        assert "hugepages-2Mi" not in lim


# --------------------------------------------------------------------------- #
# Decimal -> int coercion: the `decimal * float` bug fix (gpu_count = int(...))
# --------------------------------------------------------------------------- #
class TestDecimalCoercion:
    def test_decimal_gpu_count_limits_match_int(self):
        # DynamoDB hands numbers back as Decimal; the function must coerce first.
        assert index.get_pod_resource_limits(Decimal("2"), "h100") == \
            index.get_pod_resource_limits(2, "h100")

    def test_decimal_gpu_count_requests_match_int(self):
        assert index.get_pod_resource_requests(Decimal("2"), "h100") == \
            index.get_pod_resource_requests(2, "h100")

    def test_decimal_does_not_raise(self):
        # Pre-fix this raised: unsupported operand type(s) for *: Decimal and float
        lim = index.get_pod_resource_limits(Decimal("4"), "h100")
        assert lim["nvidia.com/gpu"] == "4"

    def test_decimal_count_value_is_int_string(self):
        lim = index.get_pod_resource_limits(Decimal("8"), "b200")
        # str(int(Decimal("8"))) == "8", never "8.0" or str(Decimal).
        assert lim["nvidia.com/gpu"] == "8"

    def test_decimal_truncates_like_int(self):
        # int(Decimal("2")) == 2 — count rendered without any decimal point.
        lim = index.get_pod_resource_limits(Decimal("2"), "h100")
        assert "." not in lim["nvidia.com/gpu"]


# --------------------------------------------------------------------------- #
# EFA gating: only multinode + full-node, never t4-small / MIG / cpu
# --------------------------------------------------------------------------- #
class TestEfaGating:
    def test_efa_added_for_full_node_multinode_limits(self):
        lim = index.get_pod_resource_limits(8, "h100", is_multinode=True)
        assert lim["vpc.amazonaws.com/efa"] == "32"  # h100 efa_count
        assert lim["hugepages-2Mi"] == "5120Mi"

    def test_efa_added_for_full_node_multinode_requests(self):
        req = index.get_pod_resource_requests(8, "h100", is_multinode=True)
        assert req["vpc.amazonaws.com/efa"] == "32"
        assert req["hugepages-2Mi"] == "5120Mi"

    def test_no_efa_when_not_multinode(self):
        lim = index.get_pod_resource_limits(8, "h100", is_multinode=False)
        assert "vpc.amazonaws.com/efa" not in lim

    def test_no_efa_for_partial_node(self):
        # multinode but not the whole node (4 of 8) => no EFA.
        lim = index.get_pod_resource_limits(4, "h100", is_multinode=True)
        assert "vpc.amazonaws.com/efa" not in lim
        assert "hugepages-2Mi" not in lim

    def test_t4_small_excluded_even_when_full_multinode(self):
        # t4-small max_gpus==1, so gpu_count==max_gpus is satisfied, but the
        # explicit t4-small exclusion still blocks EFA.
        lim = index.get_pod_resource_limits(1, "t4-small", is_multinode=True)
        assert "vpc.amazonaws.com/efa" not in lim

    def test_mig_full_node_multinode_no_efa(self):
        # mig-2g max_gpus==8; full + multinode but MIG is excluded.
        lim = index.get_pod_resource_limits(8, "h100-mig-2g", is_multinode=True)
        assert "vpc.amazonaws.com/efa" not in lim

    def test_efa_count_per_gpu_type(self):
        # a100 efa_count=4, b300 efa_count=8 — value pulled from GPU_CONFIG.
        a100 = index.get_pod_resource_limits(8, "a100", is_multinode=True)
        b300 = index.get_pod_resource_limits(8, "b300", is_multinode=True)
        assert a100["vpc.amazonaws.com/efa"] == "4"
        assert b300["vpc.amazonaws.com/efa"] == "8"

    def test_pod_uses_efa_helper_agreement(self):
        # _pod_uses_efa should agree with the presence of the efa key in limits,
        # except it omits the MIG/cpu guards present in the limits function.
        assert index._pod_uses_efa(8, "h100", is_multinode=True) is True
        assert index._pod_uses_efa(8, "h100", is_multinode=False) is False
        assert index._pod_uses_efa(4, "h100", is_multinode=True) is False
        assert index._pod_uses_efa(1, "t4-small", is_multinode=True) is False


# --------------------------------------------------------------------------- #
# Edge cases: zero GPUs, unknown SKU, default config
# --------------------------------------------------------------------------- #
class TestEdgeCases:
    def test_zero_gpu_count_yields_empty_limits(self):
        # gpu_count == 0 for a GPU SKU short-circuits the GPU branch entirely.
        assert index.get_pod_resource_limits(0, "h100") == {}

    def test_zero_gpu_count_yields_empty_requests(self):
        assert index.get_pod_resource_requests(0, "h100") == {}

    def test_zero_gpu_count_no_efa_even_multinode(self):
        # use_efa requires gpu_count == max_gpus; 0 != 8.
        assert index.get_pod_resource_limits(0, "h100", is_multinode=True) == {}

    def test_unknown_sku_uses_default_config(self):
        # GPU_CONFIG_DEFAULT: max_gpus=4, cpus=48, memory_gb=192, nvidia.com/gpu.
        lim = index.get_pod_resource_limits(1, "totally-unknown")
        # ratio 1/4: cpu=min(48,int(48*0.25*1.5)=18)=18, mem=int(192*0.25)=48
        assert lim == {"nvidia.com/gpu": "1", "cpu": "18", "memory": "48Gi"}

    def test_unknown_sku_default_resource_name(self):
        req = index.get_pod_resource_requests(2, "definitely-not-a-real-gpu")
        assert "nvidia.com/gpu" in req

    def test_return_type_is_dict(self):
        assert isinstance(index.get_pod_resource_limits(1, "h100"), dict)
        assert isinstance(index.get_pod_resource_requests(1, "h100"), dict)
