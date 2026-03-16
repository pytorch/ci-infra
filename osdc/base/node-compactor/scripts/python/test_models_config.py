"""Tests for Config.from_env() and NodeState computed properties."""

import math
import os
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta

from models import Config, NodeState, PodInfo

# ============================================================================
# Helpers
# ============================================================================

NOW = datetime.now(UTC)
GiB = 1024**3


def make_node(
    name: str = "node-1",
    nodepool: str = "default",
    cpu: float = 16.0,
    mem: int = 64 * GiB,
    is_tainted: bool = False,
    creation_time: datetime | None = None,
    pods: list[PodInfo] | None = None,
) -> NodeState:
    ns = NodeState(
        name=name,
        nodepool=nodepool,
        allocatable_cpu=cpu,
        allocatable_memory=mem,
        creation_time=creation_time or NOW - timedelta(hours=1),
        is_tainted=is_tainted,
    )
    if pods:
        ns.pods = pods
    return ns


def make_pod(
    name: str = "pod",
    cpu: float = 1.0,
    mem: int = 4 * GiB,
    node_name: str = "node-1",
    is_daemonset: bool = False,
    start_time: datetime | None = None,
) -> PodInfo:
    return PodInfo(
        name=name,
        namespace="default",
        cpu_request=cpu,
        memory_request=mem,
        node_name=node_name,
        is_daemonset=is_daemonset,
        start_time=start_time,
    )


# ============================================================================
# Config.from_env() tests
# ============================================================================


class TestConfigFromEnvDefaults(unittest.TestCase):
    """Config.from_env() with no env vars set returns defaults."""

    def test_defaults(self):
        # Clear all COMPACTOR_ env vars to ensure defaults
        env = {k: v for k, v in os.environ.items() if not k.startswith("COMPACTOR_")}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            cfg = Config.from_env()

        self.assertEqual(cfg.interval, 20)
        self.assertEqual(cfg.max_uptime_hours, 48)
        self.assertEqual(cfg.nodepool_label, "osdc.io/node-compactor")
        self.assertEqual(cfg.taint_key, "node-compactor.osdc.io/consolidating")
        self.assertEqual(cfg.min_nodes, 1)
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.taint_cooldown, 300)


class TestConfigFromEnvOverrides(unittest.TestCase):
    """Config.from_env() reads each env var and coerces types."""

    def _from_env(self, **env_vars):
        env = {k: v for k, v in os.environ.items() if not k.startswith("COMPACTOR_")}
        env.update(env_vars)
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return Config.from_env()

    def test_interval_override(self):
        cfg = self._from_env(COMPACTOR_INTERVAL="60")
        self.assertEqual(cfg.interval, 60)

    def test_max_uptime_hours_override(self):
        cfg = self._from_env(COMPACTOR_MAX_UPTIME_HOURS="72")
        self.assertEqual(cfg.max_uptime_hours, 72)

    def test_nodepool_label_override(self):
        cfg = self._from_env(COMPACTOR_NODEPOOL_LABEL="custom/label")
        self.assertEqual(cfg.nodepool_label, "custom/label")

    def test_taint_key_override(self):
        cfg = self._from_env(COMPACTOR_TAINT_KEY="custom.io/taint")
        self.assertEqual(cfg.taint_key, "custom.io/taint")

    def test_min_nodes_override(self):
        cfg = self._from_env(COMPACTOR_MIN_NODES="3")
        self.assertEqual(cfg.min_nodes, 3)

    def test_taint_cooldown_override(self):
        cfg = self._from_env(COMPACTOR_TAINT_COOLDOWN="600")
        self.assertEqual(cfg.taint_cooldown, 600)

    def test_dry_run_true(self):
        cfg = self._from_env(COMPACTOR_DRY_RUN="true")
        self.assertTrue(cfg.dry_run)

    def test_dry_run_yes(self):
        cfg = self._from_env(COMPACTOR_DRY_RUN="yes")
        self.assertTrue(cfg.dry_run)

    def test_dry_run_one(self):
        cfg = self._from_env(COMPACTOR_DRY_RUN="1")
        self.assertTrue(cfg.dry_run)

    def test_dry_run_True_uppercase(self):
        cfg = self._from_env(COMPACTOR_DRY_RUN="True")
        self.assertTrue(cfg.dry_run)

    def test_dry_run_false(self):
        cfg = self._from_env(COMPACTOR_DRY_RUN="false")
        self.assertFalse(cfg.dry_run)

    def test_dry_run_arbitrary_string_is_false(self):
        cfg = self._from_env(COMPACTOR_DRY_RUN="nope")
        self.assertFalse(cfg.dry_run)

    def test_config_is_frozen(self):
        cfg = self._from_env()
        with self.assertRaises(AttributeError):
            cfg.interval = 99


# ============================================================================
# NodeState computed properties tests
# ============================================================================


class TestNodeStateWorkloadPods(unittest.TestCase):
    """Tests for NodeState.workload_pods and workload_pod_count."""

    def test_no_pods(self):
        node = make_node()
        self.assertEqual(node.workload_pods, [])
        self.assertEqual(node.workload_pod_count, 0)

    def test_only_daemonset_pods(self):
        pods = [
            make_pod(name="ds-1", is_daemonset=True),
            make_pod(name="ds-2", is_daemonset=True),
        ]
        node = make_node(pods=pods)
        self.assertEqual(node.workload_pods, [])
        self.assertEqual(node.workload_pod_count, 0)

    def test_only_workload_pods(self):
        pods = [make_pod(name="w-1"), make_pod(name="w-2")]
        node = make_node(pods=pods)
        self.assertEqual(len(node.workload_pods), 2)
        self.assertEqual(node.workload_pod_count, 2)

    def test_mixed_pods(self):
        pods = [
            make_pod(name="w-1"),
            make_pod(name="ds-1", is_daemonset=True),
            make_pod(name="w-2"),
        ]
        node = make_node(pods=pods)
        self.assertEqual(node.workload_pod_count, 2)
        names = [p.name for p in node.workload_pods]
        self.assertEqual(names, ["w-1", "w-2"])


class TestNodeStateDaemonsetResources(unittest.TestCase):
    """Tests for NodeState.daemonset_cpu and daemonset_memory."""

    def test_no_pods(self):
        node = make_node()
        self.assertAlmostEqual(node.daemonset_cpu, 0.0)
        self.assertEqual(node.daemonset_memory, 0)

    def test_no_daemonset_pods(self):
        pods = [make_pod(name="w-1", cpu=4.0, mem=8 * GiB)]
        node = make_node(pods=pods)
        self.assertAlmostEqual(node.daemonset_cpu, 0.0)
        self.assertEqual(node.daemonset_memory, 0)

    def test_daemonset_pods_summed(self):
        pods = [
            make_pod(name="ds-1", cpu=0.5, mem=1 * GiB, is_daemonset=True),
            make_pod(name="ds-2", cpu=0.25, mem=2 * GiB, is_daemonset=True),
            make_pod(name="w-1", cpu=4.0, mem=8 * GiB),
        ]
        node = make_node(pods=pods)
        self.assertAlmostEqual(node.daemonset_cpu, 0.75)
        self.assertEqual(node.daemonset_memory, 3 * GiB)


class TestNodeStateUtilization(unittest.TestCase):
    """Tests for cpu_utilization, memory_utilization, and utilization."""

    def test_zero_allocatable_cpu(self):
        node = make_node(cpu=0.0)
        node.pods = [make_pod(cpu=1.0)]
        self.assertAlmostEqual(node.cpu_utilization, 0.0)

    def test_zero_allocatable_memory(self):
        node = make_node(mem=0)
        node.pods = [make_pod(mem=1 * GiB)]
        self.assertAlmostEqual(node.memory_utilization, 0.0)

    def test_cpu_utilization_partial(self):
        node = make_node(cpu=16.0)
        node.pods = [make_pod(cpu=4.0)]
        self.assertAlmostEqual(node.cpu_utilization, 0.25)

    def test_memory_utilization_partial(self):
        node = make_node(mem=64 * GiB)
        node.pods = [make_pod(mem=16 * GiB)]
        self.assertAlmostEqual(node.memory_utilization, 0.25)

    def test_utilization_max_of_cpu_and_memory(self):
        node = make_node(cpu=16.0, mem=64 * GiB)
        # CPU: 8/16 = 0.5, Memory: 48/64 = 0.75 -> utilization = 0.75
        node.pods = [make_pod(cpu=8.0, mem=48 * GiB)]
        self.assertAlmostEqual(node.utilization, 0.75)

    def test_utilization_excludes_daemonset_pods(self):
        node = make_node(cpu=16.0, mem=64 * GiB)
        node.pods = [
            make_pod(cpu=2.0, mem=8 * GiB, is_daemonset=True),
            make_pod(cpu=4.0, mem=16 * GiB),
        ]
        # cpu_utilization = workload only: 4/16 = 0.25
        # memory_utilization = workload only: 16/64 = 0.25
        self.assertAlmostEqual(node.cpu_utilization, 0.25)
        self.assertAlmostEqual(node.memory_utilization, 0.25)
        self.assertAlmostEqual(node.utilization, 0.25)

    def test_no_pods_zero_utilization(self):
        node = make_node(cpu=16.0, mem=64 * GiB)
        self.assertAlmostEqual(node.utilization, 0.0)


class TestNodeStateUptimeHours(unittest.TestCase):
    """Tests for NodeState.uptime_hours."""

    def test_uptime_one_hour(self):
        node = make_node(creation_time=datetime.now(UTC) - timedelta(hours=1))
        self.assertAlmostEqual(node.uptime_hours, 1.0, places=1)

    def test_uptime_two_days(self):
        node = make_node(creation_time=datetime.now(UTC) - timedelta(days=2))
        self.assertAlmostEqual(node.uptime_hours, 48.0, places=0)

    def test_uptime_just_created(self):
        node = make_node(creation_time=datetime.now(UTC))
        self.assertAlmostEqual(node.uptime_hours, 0.0, places=1)


class TestNodeStateYoungestPodAgeSeconds(unittest.TestCase):
    """Tests for NodeState.youngest_pod_age_seconds."""

    def test_no_workload_pods_returns_inf(self):
        node = make_node()
        self.assertEqual(node.youngest_pod_age_seconds, math.inf)

    def test_only_daemonset_pods_returns_inf(self):
        node = make_node(
            pods=[
                make_pod(name="ds-1", is_daemonset=True, start_time=NOW - timedelta(minutes=5)),
            ]
        )
        self.assertEqual(node.youngest_pod_age_seconds, math.inf)

    def test_single_workload_pod(self):
        start = datetime.now(UTC) - timedelta(minutes=10)
        node = make_node(pods=[make_pod(start_time=start)])
        age = node.youngest_pod_age_seconds
        self.assertAlmostEqual(age, 600, delta=5)

    def test_multiple_workload_pods_returns_youngest(self):
        old_start = datetime.now(UTC) - timedelta(hours=2)
        young_start = datetime.now(UTC) - timedelta(minutes=5)
        node = make_node(
            pods=[
                make_pod(name="old", start_time=old_start),
                make_pod(name="young", start_time=young_start),
            ]
        )
        age = node.youngest_pod_age_seconds
        self.assertAlmostEqual(age, 300, delta=5)

    def test_pod_without_start_time_skipped(self):
        start = datetime.now(UTC) - timedelta(minutes=15)
        node = make_node(
            pods=[
                make_pod(name="no-start", start_time=None),
                make_pod(name="has-start", start_time=start),
            ]
        )
        age = node.youngest_pod_age_seconds
        self.assertAlmostEqual(age, 900, delta=5)

    def test_all_pods_without_start_time_returns_inf(self):
        node = make_node(
            pods=[
                make_pod(name="p1", start_time=None),
                make_pod(name="p2", start_time=None),
            ]
        )
        self.assertEqual(node.youngest_pod_age_seconds, math.inf)


if __name__ == "__main__":
    unittest.main()
