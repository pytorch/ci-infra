"""Smoke tests for the runner-hooks-warmer DaemonSet.

Validates that the runner-hooks-warmer DaemonSet — which downloads the patched
runner-container-hooks zip onto each runner-pod pool node (c7i-runner for x86
runners, arm64-runner for arm64 runners) — is deployed and healthy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from helpers import assert_daemonset_healthy, filter_daemonsets

pytestmark = [pytest.mark.live]

NAMESPACE = "arc-runners"
RUNNER_POD_POOLS = {"c7i-runner", "arm64-runner"}
# A node that just joined may not have its warmer pod yet; don't count it.
NEW_NODE_GRACE = timedelta(minutes=5)


def _is_ready(node: dict) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in node.get("status", {}).get("conditions", [])
    )


class TestHooksWarmer:
    """Verify the runner-hooks-warmer DaemonSet is deployed and ready."""

    def _get_ds(self, all_daemonsets) -> dict:
        """Return the hooks-warmer DaemonSet dict from batch data."""
        ds_list = filter_daemonsets(all_daemonsets, namespace=NAMESPACE, name="runner-hooks-warmer")
        assert len(ds_list) >= 1, "runner-hooks-warmer DaemonSet not found"
        return ds_list[0]

    @staticmethod
    def _pod_spec(ds: dict) -> dict:
        """Safely extract spec.template.spec from a DaemonSet."""
        pod_spec = ds.get("spec", {}).get("template", {}).get("spec", {})
        assert pod_spec, "DaemonSet spec.template.spec is empty or missing"
        return pod_spec

    def test_hooks_warmer_daemonset_ready(self, all_daemonsets, all_nodes, enabled_modules) -> None:
        """DaemonSet must have all pods ready."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        assert_daemonset_healthy(all_daemonsets, all_nodes, NAMESPACE, name="runner-hooks-warmer", allow_zero=True)

    def test_priority_class(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must use system-node-critical priority to run on all nodes."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        priority = pod_spec.get("priorityClassName")
        assert priority == "system-node-critical", f"Expected priorityClassName=system-node-critical, got {priority!r}"

    def test_node_affinity_targets_runner_pod_pools(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must target exactly the runner-pod pools via a node-fleet affinity."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        terms = (
            pod_spec.get("affinity", {})
            .get("nodeAffinity", {})
            .get("requiredDuringSchedulingIgnoredDuringExecution", {})
            .get("nodeSelectorTerms", [])
        )
        fleets = {
            value
            for term in terms
            for expr in term.get("matchExpressions", [])
            if expr.get("key") == "node-fleet" and expr.get("operator") == "In"
            for value in expr.get("values", [])
        }
        assert fleets == RUNNER_POD_POOLS, f"Expected node-fleet In {sorted(RUNNER_POD_POOLS)}, got {sorted(fleets)}"

    def test_tolerates_runner_pod_pool_taints(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must tolerate both runner-pod pools' node-fleet + instance-type taints."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        tolerations = pod_spec.get("tolerations", [])
        tolerated_keys = {t.get("key") for t in tolerations}

        required_taints = {"node-fleet", "instance-type"}
        missing = required_taints - tolerated_keys
        assert not missing, f"DaemonSet missing tolerations for taints: {missing}"

        # The node-fleet tolerations must be value-scoped to the runner-pod pools so
        # the warmer cannot land on other pools (e.g. node-fleet=g4dn workflow nodes).
        node_fleet_tols = [t for t in tolerations if t.get("key") == "node-fleet"]
        assert all(t.get("operator") == "Equal" for t in node_fleet_tols), (
            f"node-fleet tolerations must be Equal-scoped, got {node_fleet_tols!r}"
        )
        tolerated_pools = {t.get("value") for t in node_fleet_tols}
        assert tolerated_pools == RUNNER_POD_POOLS, (
            f"Expected node-fleet tolerations for {sorted(RUNNER_POD_POOLS)}, got {sorted(tolerated_pools)}"
        )

    def test_runs_on_every_runner_pod_pool_node(self, all_daemonsets, all_nodes, all_pods, enabled_modules) -> None:
        """Every live node in a runner-pod pool must have a warmer pod, whatever its arch.

        A runner pod waits in its init container until the hooks land on the node, so a
        pool node without a warmer pod would stall every runner scheduled onto it.
        """
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        settled_before = datetime.now(UTC) - NEW_NODE_GRACE
        pool_nodes = {
            n["metadata"]["name"]
            for n in all_nodes.get("items", [])
            if n["metadata"].get("labels", {}).get("node-fleet") in RUNNER_POD_POOLS
            and _is_ready(n)
            and datetime.fromisoformat(n["metadata"]["creationTimestamp"].replace("Z", "+00:00")) < settled_before
        }
        warmer_nodes = {
            p.get("spec", {}).get("nodeName")
            for p in all_pods.get("items", [])
            if p["metadata"].get("namespace") == NAMESPACE
            and p["metadata"].get("labels", {}).get("app") == "runner-hooks-warmer"
        }
        missing = sorted(pool_nodes - warmer_nodes)
        assert not missing, f"Runner-pod pool nodes without a hooks-warmer pod: {missing}"

    def test_hostpath_volume_narrowed(self, all_daemonsets, enabled_modules) -> None:
        """Volume must mount /mnt/runner-container-hooks (not all of /mnt)."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        volumes = pod_spec.get("volumes", [])

        hooks_vol = None
        for v in volumes:
            hp = v.get("hostPath", {})
            if hp.get("path") == "/mnt/runner-container-hooks":
                hooks_vol = v
                break

        assert hooks_vol is not None, "No hostPath volume for /mnt/runner-container-hooks found"

    def test_container_volume_mount(self, all_daemonsets, enabled_modules) -> None:
        """At least one container must mount the hooks dir at /mnt/runner-container-hooks."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        containers = pod_spec.get("containers", [])
        assert len(containers) >= 1, "No containers in DaemonSet"

        # Search all containers, not just the first one
        all_mounts = []
        for c in containers:
            for m in c.get("volumeMounts", []):
                all_mounts.append(m["mountPath"])
                if m["mountPath"] == "/mnt/runner-container-hooks":
                    return  # Found it

        pytest.fail(f"No container has volumeMount at /mnt/runner-container-hooks. All mounts: {all_mounts}")
