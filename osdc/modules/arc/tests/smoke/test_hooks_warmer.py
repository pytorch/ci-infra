"""Smoke tests for the runner-hooks-warmer DaemonSet.

Validates that the runner-hooks-warmer DaemonSet — which downloads the patched
runner-container-hooks zip onto each c7i-runner node — is deployed and healthy.
"""

from __future__ import annotations

import pytest
from helpers import assert_daemonset_healthy, filter_daemonsets

pytestmark = [pytest.mark.live]

NAMESPACE = "arc-runners"


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

    def test_node_selector_targets_c7i_runner_pool(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must target the dedicated c7i-runner pool via node-fleet selector."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        node_selector = pod_spec.get("nodeSelector", {})
        assert node_selector.get("node-fleet") == "c7i-runner", (
            f"Expected nodeSelector node-fleet=c7i-runner, got {node_selector!r}"
        )

    def test_tolerates_c7i_runner_pool_taints(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must tolerate the c7i-runner pool's node-fleet + instance-type taints."""
        if "arc" not in enabled_modules:
            pytest.skip("arc module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        tolerations = pod_spec.get("tolerations", [])
        tolerated_keys = {t.get("key") for t in tolerations}

        # The warmer must NOT be schedulable on workflow pool nodes (different
        # node-fleet value).
        required_taints = {"node-fleet", "instance-type"}
        missing = required_taints - tolerated_keys
        assert not missing, f"DaemonSet missing tolerations for taints: {missing}"

        # The node-fleet toleration must be value-scoped to c7i-runner so the
        # warmer cannot land on other pools (e.g. node-fleet=g4dn workflow nodes).
        node_fleet_tols = [t for t in tolerations if t.get("key") == "node-fleet"]
        assert any(t.get("operator") == "Equal" and t.get("value") == "c7i-runner" for t in node_fleet_tols), (
            f"node-fleet toleration must be Equal/c7i-runner, got {node_fleet_tols!r}"
        )

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
