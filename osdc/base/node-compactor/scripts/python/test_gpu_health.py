"""Unit tests for GPU black-hole detection."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from gpu_health import (
    admission_failure_time,
    count_recent_failures,
    select_quarantine_nodes,
)
from models import Config, NodeState

# ============================================================================
# Helpers
# ============================================================================

NOW = datetime.now(UTC)

# Verbatim kubelet rejection message from the 2026-09-03 g5.24xlarge episode.
REAL_MESSAGE = (
    "Pod was rejected: Allocate failed due to device plugin GetPreferredAllocation "
    "rpc failed with err: rpc error: code = Unknown desc = error getting list of "
    "preferred allocation devices: unable to get device link information: error "
    "getting NVLink for devices (0, 1): failed to get nvlink remote pci info: "
    "failed to get nvlink state: GPU is lost, which is unexpected"
)


def make_config(**overrides) -> Config:
    defaults = {
        "interval": 20,
        "max_uptime_hours": 48,
        "nodepool_label": "osdc.io/node-compactor",
        "taint_key": "node-compactor.osdc.io/consolidating",
        "min_nodes": 1,
        "dry_run": False,
        "taint_cooldown": 300,
        "min_node_age": 900,
        "fleet_cooldown": 120,
        "taint_rate": 1.0,
        "spare_capacity_nodes": 2,
        "spare_capacity_ratio": 0.15,
        "spare_capacity_threshold": 0.4,
        "capacity_reservation_nodes": 0,
        "peak_window_seconds": 2700,
        "pending_pod_max_age_seconds": 14400,
        "pending_pod_min_age_seconds": 0,
        "gpu_quarantine_enabled": True,
        "gpu_quarantine_threshold": 3,
        "gpu_quarantine_window_seconds": 300,
        "gpu_quarantine_max_fleet_ratio": 0.2,
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_pod(reason: str | None, message: str | None, created: datetime | None = NOW):
    pod = MagicMock()
    pod.status.reason = reason
    pod.status.message = message
    pod.metadata.creationTimestamp = created
    return pod


def make_gpu_node(
    name: str,
    failures: int = 0,
    age_seconds: int = 10,
    gpus: int = 4,
    fleet: str = "g5",
    quarantined: bool = False,
) -> NodeState:
    return NodeState(
        name=name,
        nodepool=f"{fleet}-24xlarge",
        allocatable_cpu=96,
        allocatable_memory=384 * 1024**3,
        allocatable_gpu=gpus,
        creation_time=NOW - timedelta(hours=2),
        labels={"node-fleet": fleet},
        is_gpu_quarantined=quarantined,
        admission_failures=[NOW - timedelta(seconds=age_seconds)] * failures,
    )


def fleet_key(ns: NodeState) -> str:
    return ns.labels.get("node-fleet") or ns.nodepool


# ============================================================================
# admission_failure_time
# ============================================================================


class TestAdmissionFailureTime:
    def test_matches_real_kubelet_message(self):
        pod = make_pod("UnexpectedAdmissionError", REAL_MESSAGE)
        assert admission_failure_time(pod) == NOW

    def test_ignores_other_reasons(self):
        pod = make_pod("Evicted", REAL_MESSAGE)
        assert admission_failure_time(pod) is None

    def test_ignores_uncategorized_failures_without_the_signature(self):
        """UnexpectedAdmissionError is kubelet's catch-all bucket.

        Matching on reason alone would quarantine nodes for unrelated
        admission errors, so the message signature has to carry the match.
        """
        pod = make_pod(
            "UnexpectedAdmissionError",
            "Pod was rejected: Allocate failed due to some other thing, which is unexpected",
        )
        assert admission_failure_time(pod) is None

    def test_ignores_topology_affinity_error(self):
        """TopologyAffinityError is a distinct typed reason, not this fault."""
        pod = make_pod("TopologyAffinityError", "Resources cannot be allocated with Topology locality")
        assert admission_failure_time(pod) is None

    def test_tolerates_missing_message(self):
        assert admission_failure_time(make_pod("UnexpectedAdmissionError", None)) is None

    def test_tolerates_missing_status(self):
        pod = MagicMock()
        pod.status = None
        assert admission_failure_time(pod) is None


# ============================================================================
# count_recent_failures
# ============================================================================


class TestCountRecentFailures:
    def test_counts_within_window(self):
        ns = make_gpu_node("n1", failures=5, age_seconds=10)
        assert count_recent_failures(ns, make_config(), NOW) == 5

    def test_excludes_stale_failures(self):
        """Rejected pod objects linger in Failed phase and are not always reaped.

        An unbounded count would keep a node quarantined off corpses long
        after the fault, so anything outside the window must not count.
        """
        ns = make_gpu_node("n1", failures=9, age_seconds=3600)
        assert count_recent_failures(ns, make_config(), NOW) == 0

    def test_boundary_is_inclusive(self):
        ns = make_gpu_node("n1", failures=1, age_seconds=300)
        assert count_recent_failures(ns, make_config(gpu_quarantine_window_seconds=300), NOW) == 1


# ============================================================================
# select_quarantine_nodes
# ============================================================================


class TestSelectQuarantineNodes:
    def test_quarantines_node_over_threshold(self):
        states = {"bad": make_gpu_node("bad", failures=3)}
        selected, counts = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert selected == {"bad"}
        assert counts == {"bad": 3}

    def test_leaves_node_under_threshold(self):
        states = {"n1": make_gpu_node("n1", failures=2)}
        selected, counts = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert selected == set()
        # Still reported, so the metric shows pressure before the taint lands.
        assert counts == {"n1": 2}

    def test_ignores_non_gpu_nodes(self):
        """A CPU node cannot hit this fault; its failures mean something else."""
        cpu_node = make_gpu_node("cpu-1", failures=10, gpus=0, fleet="c7a")
        selected, counts = select_quarantine_nodes({"cpu-1": cpu_node}, make_config(), fleet_key, NOW)
        assert selected == set()
        assert counts == {}

    def test_skips_already_quarantined(self):
        states = {"bad": make_gpu_node("bad", failures=9, quarantined=True)}
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert selected == set()

    def test_disabled_returns_nothing(self):
        states = {"bad": make_gpu_node("bad", failures=99)}
        selected, counts = select_quarantine_nodes(states, make_config(gpu_quarantine_enabled=False), fleet_key, NOW)
        assert selected == set()
        assert counts == {}

    def test_only_the_failing_node_in_a_healthy_fleet(self):
        states = {f"ok-{i}": make_gpu_node(f"ok-{i}") for i in range(9)}
        states["bad"] = make_gpu_node("bad", failures=4)
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert selected == {"bad"}

    def test_fleet_cap_limits_mass_quarantine(self):
        """A fleet-wide fault must not cordon the whole fleet.

        10 nodes at ratio 0.2 caps total quarantine at 2, even though all 10
        are failing — turning a degraded fleet into no fleet is worse.
        """
        states = {f"n{i}": make_gpu_node(f"n{i}", failures=5) for i in range(10)}
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert len(selected) == 2

    def test_fleet_cap_counts_existing_quarantines(self):
        states = {f"n{i}": make_gpu_node(f"n{i}", failures=5) for i in range(10)}
        states["n0"] = make_gpu_node("n0", failures=5, quarantined=True)
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        # Cap of 2 with one already quarantined leaves room for exactly one.
        assert len(selected) == 1

    def test_cap_allows_at_least_one(self):
        """Small fleets would otherwise round the cap down to zero."""
        states = {"only": make_gpu_node("only", failures=5)}
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert selected == {"only"}

    def test_cap_is_per_fleet(self):
        states = {f"g5-{i}": make_gpu_node(f"g5-{i}", failures=5, fleet="g5") for i in range(5)}
        states.update({f"g6-{i}": make_gpu_node(f"g6-{i}", failures=5, fleet="g6") for i in range(5)})
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert len([n for n in selected if n.startswith("g5-")]) == 1
        assert len([n for n in selected if n.startswith("g6-")]) == 1

    def test_worst_offender_wins_when_capped(self):
        states = {f"n{i}": make_gpu_node(f"n{i}", failures=3) for i in range(10)}
        states["worst"] = make_gpu_node("worst", failures=50)
        selected, _ = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert "worst" in selected

    def test_stale_failures_do_not_trigger(self):
        states = {"n1": make_gpu_node("n1", failures=99, age_seconds=7200)}
        selected, counts = select_quarantine_nodes(states, make_config(), fleet_key, NOW)
        assert selected == set()
        assert counts == {}


# ============================================================================
# Regression: the 2026-09-03 episode
# ============================================================================


class TestSep3Episode:
    """The real incident: one lost A10G on a 4-GPU g5.24xlarge.

    The node stayed Ready and kept advertising 4 allocatable GPUs while its
    kubelet rejected 554 pods over 1h46m. Detection has to come from the pod
    corpses, because nothing on the node itself was ever wrong.
    """

    def test_black_hole_is_quarantined_within_the_first_handful_of_pods(self):
        node = make_gpu_node("ip-10-4-164-212.us-east-2.compute.internal", failures=0, gpus=4)
        # Observed rate was roughly one rejection every 6-13 seconds.
        node.admission_failures = [NOW - timedelta(seconds=s) for s in (26, 13, 0)]
        healthy = {f"ok-{i}": make_gpu_node(f"ok-{i}") for i in range(19)}
        states = {node.name: node, **healthy}

        selected, counts = select_quarantine_nodes(states, make_config(), fleet_key, NOW)

        assert selected == {node.name}
        assert counts[node.name] == 3
        # The other 19 g5 nodes were healthy and must be untouched.
        assert all(n.startswith("ok-") is False for n in selected)
