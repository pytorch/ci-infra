"""Smoke tests for ARC runner scale sets.

Validates that runner definitions are well-formed and that the expected
ConfigMaps + Helm releases exist in the cluster for each definition.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml
from helpers import filter_pods, find_helm_release, run_kubectl
from runner_defs import arc_runners_module_names, def_for_listener_pod, load_defs_by_name, load_runner_defs

pytestmark = [pytest.mark.live]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NAMESPACE = "arc-runners"
LISTENER_NAMESPACE = "arc-systems"
LISTENER_LABELS = {"app.kubernetes.io/component": "runner-scale-set-listener"}
LISTENER_CONTAINER_NAME = "listener"
MODULE_LABEL_KEY = "osdc.io/module"
REQUIRED_FIELDS = {"name", "instance_type", "disk_size", "vcpu", "memory"}


def _normalize_name(name: str) -> str:
    return name.replace(".", "-").replace("_", "-")


# Backwards-compatible local alias — tests below pass `upstream_dir` and the
# runner-def loader lives in runner_defs.py. Keep this thin shim so the call
# sites here read naturally.
def _load_all_defs(upstream_dir: Path, modules: Iterable[str] | None = None) -> list[dict]:
    return load_runner_defs(upstream_dir, modules)


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

        # Scope to arc-runners* modules actually enabled on this cluster — defs
        # from unenabled-but-present-in-codebase variants would otherwise be
        # expected as CMs that genuinely don't exist on this cluster.
        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)

        defs = _load_all_defs(upstream_dir, enabled_arc)
        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL_KEY, "-o", "json"], namespace=NAMESPACE)
        cm_names = {
            item["metadata"]["name"]
            for item in result.get("items", [])
            if item.get("metadata", {}).get("labels", {}).get(MODULE_LABEL_KEY) in enabled_arc
        }

        missing = []
        for d in defs:
            expected = f"arc-runner-hook-{_normalize_name(d['name'])}"
            if expected not in cm_names:
                missing.append(expected)

        assert not missing, f"Missing ConfigMaps for runner defs: {missing}"


# ============================================================================
# Live: Runner ConfigMap Environment Variables
# ============================================================================


class TestRunnerConfigMapEnvVars:
    """Verify ConfigMaps contain required pypi-cache environment variables."""

    REQUIRED_ENV_VARS: frozenset[str] = frozenset(
        {
            "PIP_INDEX_URL",
            "PIP_TRUSTED_HOST",
            "PIP_EXTRA_INDEX_URL",
            "UV_DEFAULT_INDEX",
            "UV_INSECURE_HOST",
            "UV_INDEX",
            "UV_INDEX_STRATEGY",
        }
    )

    def test_pypi_cache_env_vars_present(self, upstream_dir: Path, enabled_modules: list[str]) -> None:
        """Each runner ConfigMap's job-pod.yaml has all pypi-cache env vars."""
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        # Scope to arc-runners* modules actually enabled on this cluster — defs
        # from unenabled-but-present-in-codebase variants would otherwise be
        # expected as CMs that genuinely don't exist on this cluster.
        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)

        defs = _load_all_defs(upstream_dir, enabled_arc)
        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL_KEY, "-o", "json"], namespace=NAMESPACE)
        items = [
            item
            for item in result.get("items", [])
            if item.get("metadata", {}).get("labels", {}).get(MODULE_LABEL_KEY) in enabled_arc
        ]

        missing_vars: list[str] = []
        for d in defs:
            cm_name = f"arc-runner-hook-{_normalize_name(d['name'])}"
            cm = None
            for item in items:
                if item["metadata"]["name"] == cm_name:
                    cm = item
                    break
            if cm is None:
                continue  # TestRunnerConfigMaps covers missing CMs

            job_pod_yaml = cm.get("data", {}).get("job-pod.yaml", "")
            if not job_pod_yaml:
                missing_vars.append(f"{cm_name}: no job-pod.yaml data")
                continue

            pod_data = yaml.safe_load(job_pod_yaml)
            containers = pod_data.get("spec", {}).get("containers", [])
            if not containers:
                missing_vars.append(f"{cm_name}: no containers in job-pod.yaml")
                continue

            env_names = {e["name"] for e in containers[0].get("env", [])}
            missing = self.REQUIRED_ENV_VARS - env_names
            if missing:
                missing_vars.append(f"{cm_name}: missing {missing}")

        assert not missing_vars, "ConfigMaps with missing pypi-cache env vars:\n" + "\n".join(missing_vars)


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

        # Scope to arc-runners* modules actually enabled on this cluster — defs
        # from unenabled-but-present-in-codebase variants would otherwise be
        # expected as releases that genuinely don't exist on this cluster.
        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)

        defs = _load_all_defs(upstream_dir, enabled_arc)
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

        # Scope to arc-runners* modules actually enabled on this cluster — defs
        # from unenabled-but-present-in-codebase variants would otherwise be
        # expected as CMs that genuinely don't exist on this cluster.
        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)

        defs = _load_all_defs(upstream_dir, enabled_arc)
        expected_cms = {f"arc-runner-hook-{_normalize_name(d['name'])}" for d in defs}

        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL_KEY, "-o", "json"], namespace=NAMESPACE)
        actual_cms = {
            item["metadata"]["name"]
            for item in result.get("items", [])
            if item.get("metadata", {}).get("labels", {}).get(MODULE_LABEL_KEY) in enabled_arc
        }

        stale = actual_cms - expected_cms
        assert not stale, f"Stale ConfigMaps (no matching def): {stale}"


# ============================================================================
# Live: Listener Pod Capacity-Aware Env Vars
# ============================================================================


def _listener_env_from_generated_yaml(generated_doc: dict) -> dict[str, dict]:
    """Extract the listener container's env list (as a {name: env_entry} map).

    Looks up ``listenerTemplate.spec.containers[0].env`` from a parsed
    generated YAML. Uses ``[0]`` because the chart values define exactly one
    container under ``listenerTemplate``; a missing/empty env list returns ``{}``
    so callers see "expected env vars missing" rather than a KeyError.
    """
    containers = generated_doc.get("listenerTemplate", {}).get("spec", {}).get("containers", []) or []
    if not containers:
        return {}
    return {e["name"]: e for e in containers[0].get("env", []) or []}


def _capacity_aware_env(env_by_name: dict[str, dict]) -> dict[str, dict]:
    """Filter an env-by-name map down to CAPACITY_AWARE_* entries."""
    return {name: entry for name, entry in env_by_name.items() if name.startswith("CAPACITY_AWARE_")}


class TestListenerCapacityAwareEnvVars:
    """Verify ARC listener pods have CAPACITY_AWARE_* env vars matching the
    GENERATED runner YAML (post-override, post-template-substitution).

    The generated YAML is the single source of truth for what should be
    deployed: it already accounts for cluster-specific overrides such as
    ``proactive_capacity_max`` (staging) and any future generator
    transformations. Comparing the deployed listener directly against the
    runner def would re-introduce knowledge of those overrides into the test.
    """

    @staticmethod
    def _env_value_signature(entry: dict) -> tuple:
        """Hashable signature for an env entry's value source.

        For literal-value entries, returns ``("value", <str>)``. For
        ``valueFrom`` entries, returns ``("valueFrom", <normalized dict>)``
        so secret/configmap references compare structurally without trying
        to read the actual secret.
        """
        if "valueFrom" in entry and entry["valueFrom"] is not None:
            # Sort keys for deterministic comparison; valueFrom blocks are
            # small dicts (e.g. {"secretKeyRef": {...}}) — JSON-style sort is
            # cheap and stable.
            return ("valueFrom", json.dumps(entry["valueFrom"], sort_keys=True))
        # Treat missing value as empty string (matches K8s behavior).
        return ("value", entry.get("value", "") or "")

    def test_listener_env_vars_match_generated_yaml(
        self,
        all_pods: dict,
        upstream_dir: Path,
        enabled_modules: list[str],
        resolve_config,
        generated_arc_runners: dict[str, dict],
    ) -> None:
        """Each listener pod's CAPACITY_AWARE_* env vars match its generated YAML.

        For each CAPACITY_AWARE_* env var present in the generated YAML's
        listener container, the deployed pod must have an entry with the
        same value (literal) or the same valueFrom (secret/configmap ref).
        Also catches deployed env vars that exist in the pod but are absent
        from the generated YAML — surfaces drift from a stale chart install.
        """
        if "arc-runners" not in enabled_modules:
            pytest.skip("arc-runners module not enabled")

        listener_pods = filter_pods(all_pods, namespace=LISTENER_NAMESPACE, labels=LISTENER_LABELS)
        assert len(listener_pods) >= 1, (
            f"No listener pods found in '{LISTENER_NAMESPACE}' with labels {LISTENER_LABELS}"
        )

        runner_name_prefix = resolve_config("arc-runners.runner_name_prefix", "")
        # Reuse load_defs_by_name only for the listener→def mapping helper —
        # value comparisons go through generated_arc_runners.
        defs_by_name = load_defs_by_name(upstream_dir)

        problems: list[str] = []
        for pod in listener_pods:
            pod_name = pod["metadata"]["name"]
            containers = pod.get("spec", {}).get("containers", [])
            listener = next((c for c in containers if c.get("name") == LISTENER_CONTAINER_NAME), None)
            if listener is None:
                problems.append(f"{pod_name}: no '{LISTENER_CONTAINER_NAME}' container")
                continue

            def_name, _runner = def_for_listener_pod(pod, defs_by_name, runner_name_prefix)
            if def_name is None:
                problems.append(
                    f"{pod_name}: missing/invalid 'actions.github.com/scale-set-name' label "
                    f"(prefix={runner_name_prefix!r})"
                )
                continue

            generated = generated_arc_runners.get(def_name)
            if generated is None:
                problems.append(f"{pod_name}: no generated YAML found for def {def_name!r} (stale scale-set?)")
                continue

            generated_env = _capacity_aware_env(_listener_env_from_generated_yaml(generated))
            if not generated_env:
                problems.append(
                    f"{pod_name} (def={def_name}): generated YAML has no CAPACITY_AWARE_* env vars in "
                    f"listenerTemplate — template/generator regression?"
                )
                continue

            deployed_env = _capacity_aware_env({e["name"]: e for e in listener.get("env", []) or []})

            # Every CAPACITY_AWARE_* env var in the generated YAML must be in
            # the deployed pod with an identical value/valueFrom shape.
            for var, want_entry in generated_env.items():
                got_entry = deployed_env.get(var)
                if got_entry is None:
                    problems.append(f"{pod_name} (def={def_name}): missing env var {var!r}")
                    continue
                want_sig = self._env_value_signature(want_entry)
                got_sig = self._env_value_signature(got_entry)
                if want_sig != got_sig:
                    problems.append(
                        f"{pod_name} (def={def_name}): {var} mismatch — generated {want_sig!r}, deployed {got_sig!r}"
                    )

            # Catch the reverse: env vars deployed but no longer in the
            # generated YAML (chart out of sync with the latest generator).
            extra = sorted(deployed_env.keys() - generated_env.keys())
            if extra:
                problems.append(
                    f"{pod_name} (def={def_name}): unexpected CAPACITY_AWARE_* env vars not in generated YAML: {extra}"
                )

        assert not problems, "Listener CAPACITY_AWARE_* env-var coherence failures:\n" + "\n".join(problems)
