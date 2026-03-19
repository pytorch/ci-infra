"""Smoke tests for ARC runner scale sets.

Validates that runner definitions are well-formed and that the expected
ConfigMaps + Helm releases exist in the cluster for each definition.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from helpers import assert_daemonset_healthy, filter_daemonsets, find_helm_release, run_kubectl

pytestmark = [pytest.mark.live]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAMESPACE = "arc-runners"
MODULE_LABEL = "osdc.io/module=arc-runners"
REQUIRED_FIELDS = {"name", "instance_type", "disk_size", "vcpu", "memory"}


def _normalize_name(name: str) -> str:
    return name.replace(".", "-").replace("_", "-")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _defs_dir(upstream_dir: Path) -> Path:
    """Resolve the runner definitions directory, respecting env override."""
    override = os.environ.get("ARC_RUNNERS_DEFS_DIR")
    if override:
        return Path(override)
    return upstream_dir / "modules" / "arc-runners" / "defs"


def _load_all_defs(upstream_dir: Path) -> list[dict]:
    """Load all runner definition YAML files and return the runner dicts."""
    defs_path = _defs_dir(upstream_dir)
    defs = []
    for f in sorted(defs_path.glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if data and "runner" in data:
            defs.append(data["runner"])
    return defs


# ============================================================================
# Offline: Runner Definition Validation
# ============================================================================


class TestRunnerDefs:
    """Validate runner definition files are well-formed."""

    def test_defs_exist(self, upstream_dir: Path) -> None:
        """At least one runner definition exists."""
        defs = _load_all_defs(upstream_dir)
        assert len(defs) > 0, "No runner definitions found"

    def test_required_fields(self, upstream_dir: Path) -> None:
        """Every runner def has the required fields."""
        defs = _load_all_defs(upstream_dir)
        for d in defs:
            missing = REQUIRED_FIELDS - set(d.keys())
            assert not missing, f"Runner '{d.get('name', '?')}' missing fields: {missing}"

    def test_names_are_unique(self, upstream_dir: Path) -> None:
        """No duplicate runner names."""
        defs = _load_all_defs(upstream_dir)
        names = [d["name"] for d in defs]
        dupes = [n for n in names if names.count(n) > 1]
        assert not dupes, f"Duplicate runner names: {set(dupes)}"


# ============================================================================
# Live: Runner ConfigMaps
# ============================================================================


class TestRunnerConfigMaps:
    """Verify ConfigMaps exist for each runner definition."""

    def test_configmaps_for_all_defs(self, upstream_dir: Path, enabled_modules: list[str]) -> None:
        """Each runner def has a matching ConfigMap in arc-runners namespace."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        defs = _load_all_defs(upstream_dir)
        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL, "-o", "json"], namespace=NAMESPACE)
        cm_names = {item["metadata"]["name"] for item in result.get("items", [])}

        missing = []
        for d in defs:
            expected = f"arc-runner-hook-{_normalize_name(d['name'])}"
            if expected not in cm_names:
                missing.append(expected)

        assert not missing, f"Missing ConfigMaps for runner defs: {missing}"


# ============================================================================
# Live: Runner Helm Releases
# ============================================================================


class TestRunnerHelmReleases:
    """Verify Helm releases exist for each runner definition."""

    def test_helm_releases_for_all_defs(
        self, upstream_dir: Path, all_helm_releases: list[dict], enabled_modules: list[str]
    ) -> None:
        """Each runner def has a matching Helm release."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        defs = _load_all_defs(upstream_dir)
        missing = []
        for d in defs:
            release_name = f"arc-{_normalize_name(d['name'])}"
            release = find_helm_release(all_helm_releases, release_name)
            if release is None:
                missing.append(release_name)

        assert not missing, f"Missing Helm releases for runner defs: {missing}"


# ============================================================================
# Live: No Stale Runners
# ============================================================================


class TestNoStaleRunners:
    """Verify no orphaned ConfigMaps exist that don't match any runner def."""

    def test_no_stale_configmaps(self, upstream_dir: Path, enabled_modules: list[str]) -> None:
        """All ConfigMaps with the arc-runners label match a known runner def."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        defs = _load_all_defs(upstream_dir)
        expected_cms = {f"arc-runner-hook-{_normalize_name(d['name'])}" for d in defs}

        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL, "-o", "json"], namespace=NAMESPACE)
        actual_cms = {item["metadata"]["name"] for item in result.get("items", [])}

        stale = actual_cms - expected_cms
        assert not stale, f"Stale ConfigMaps (no matching def): {stale}"


# ============================================================================
# Live: Namespace
# ============================================================================


class TestArcRunnersNamespace:
    """Verify the arc-runners namespace exists with correct labels."""

    def test_namespace_exists(self, all_namespaces, enabled_modules):
        """The arc-runners namespace must exist when the module is enabled."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
        ns_names = {item["metadata"]["name"] for item in all_namespaces.get("items", [])}
        assert NAMESPACE in ns_names, f"Namespace '{NAMESPACE}' not found"

    def test_namespace_labels(self, all_namespaces, enabled_modules):
        """Namespace must have the part-of label for identification."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
        ns = None
        for item in all_namespaces.get("items", []):
            if item["metadata"]["name"] == NAMESPACE:
                ns = item
                break
        assert ns is not None, f"Namespace '{NAMESPACE}' not found"
        labels = ns.get("metadata", {}).get("labels", {})
        assert labels.get("app.kubernetes.io/part-of") == "osdc-arc-runners", (
            f"Namespace missing label app.kubernetes.io/part-of=osdc-arc-runners, got: {labels}"
        )


# ============================================================================
# Live: Hooks Warmer DaemonSet
# ============================================================================


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
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
        assert_daemonset_healthy(all_daemonsets, all_nodes, "arc-runners", name="runner-hooks-warmer", allow_zero=True)

    def test_priority_class(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must use system-node-critical priority to run on all nodes."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        priority = pod_spec.get("priorityClassName")
        assert priority == "system-node-critical", f"Expected priorityClassName=system-node-critical, got {priority!r}"

    def test_node_affinity_targets_runner_nodes(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must target workload-type=github-runner nodes via In operator."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        affinity = pod_spec.get("affinity", {})
        node_affinity = affinity.get("nodeAffinity", {})
        required = node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {})
        terms = required.get("nodeSelectorTerms", [])

        found = False
        for term in terms:
            for expr in term.get("matchExpressions", []):
                if (
                    expr.get("key") == "workload-type"
                    and expr.get("operator") == "In"
                    and "github-runner" in expr.get("values", [])
                ):
                    found = True
                    break

        assert found, "DaemonSet missing nodeAffinity for workload-type In [github-runner]"

    def test_tolerates_nodepool_taints(self, all_daemonsets, enabled_modules) -> None:
        """DaemonSet must tolerate all Karpenter nodepool taints."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
        pod_spec = self._pod_spec(self._get_ds(all_daemonsets))
        tolerations = pod_spec.get("tolerations", [])
        tolerated_keys = {t.get("key") for t in tolerations}

        # Critical taint keys that must be tolerated for the DaemonSet to
        # schedule on all runner/GPU nodes
        required_taints = {
            "instance-type",
            "nvidia.com/gpu",
            "CriticalAddonsOnly",
            "cpu-type",
            "git-cache-not-ready",
        }
        missing = required_taints - tolerated_keys
        assert not missing, f"DaemonSet missing tolerations for taints: {missing}"

    def test_hostpath_volume_narrowed(self, all_daemonsets, enabled_modules) -> None:
        """Volume must mount /mnt/runner-container-hooks (not all of /mnt)."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
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
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")
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
