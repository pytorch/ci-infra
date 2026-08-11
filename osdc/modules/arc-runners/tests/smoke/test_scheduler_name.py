from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from helpers import filter_pods, run_kubectl
from runner_defs import (
    arc_runners_module_names,
    def_for_listener_pod,
    load_defs_by_name,
    load_runner_defs,
)

pytestmark = [pytest.mark.live]

NAMESPACE = "arc-runners"
LISTENER_NAMESPACE = "arc-systems"
LISTENER_LABELS = {"app.kubernetes.io/component": "runner-scale-set-listener"}
LISTENER_CONTAINER_NAME = "listener"
MODULE_LABEL_KEY = "osdc.io/module"
SCHEDULER_MODULE = "bin-pack-scheduler"
SCHEDULER_ENV_VAR = "CAPACITY_AWARE_WORKFLOW_SCHEDULER_NAME"


def _normalize_name(name: str) -> str:
    return name.replace(".", "-").replace("_", "-")


def _effective_scheduler_name(runner_def: dict, cluster_default: str) -> str:
    own = runner_def.get("scheduler_name", "")
    return own if own else cluster_default


def _workflow_pod_from_cm_data(data: dict) -> dict | None:
    raw = data.get("job-pod.yaml", "")
    if not raw:
        return None
    parsed = yaml.safe_load(raw)
    return parsed if isinstance(parsed, dict) else None


def _skip_unless_scheduler_configured(
    enabled_modules: list[str],
    cluster_default_scheduler: str,
) -> None:
    if "arc-runners" not in enabled_modules:
        pytest.skip("arc-runners module not enabled")
    if SCHEDULER_MODULE not in enabled_modules:
        pytest.skip(f"{SCHEDULER_MODULE} module not enabled")
    if not cluster_default_scheduler:
        pytest.skip("arc-runners.scheduler_name not configured for this cluster")


class TestSchedulerName:
    def test_generated_workflow_pod_has_scheduler_name(
        self,
        upstream_dir: Path,
        enabled_modules: list[str],
        resolve_config,
    ) -> None:
        cluster_default = resolve_config("arc-runners.scheduler_name", "")
        _skip_unless_scheduler_configured(enabled_modules, cluster_default)

        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)
        defs_by_name = load_defs_by_name(upstream_dir, enabled_arc)

        modules_dir = upstream_dir / "modules"
        generated_files = []
        for module in sorted(enabled_arc):
            generated_files.extend(sorted((modules_dir / module / "generated").glob("*.yaml")))
        assert generated_files, f"No generated YAMLs found for arc-runners modules: {sorted(enabled_arc)}"

        problems: list[str] = []
        for yaml_file in generated_files:
            def_name = yaml_file.stem
            runner = defs_by_name.get(def_name)
            if runner is None:
                problems.append(f"{yaml_file}: no runner def matches stem {def_name!r}")
                continue
            expected = _effective_scheduler_name(runner, cluster_default)
            docs = list(yaml.safe_load_all(yaml_file.read_text()))
            cm_doc = next(
                (d for d in docs if isinstance(d, dict) and d.get("kind") == "ConfigMap"),
                None,
            )
            if cm_doc is None:
                problems.append(f"{yaml_file}: no ConfigMap document in generated YAML")
                continue
            pod = _workflow_pod_from_cm_data(cm_doc.get("data", {}) or {})
            if pod is None:
                problems.append(f"{yaml_file}: ConfigMap has no parseable job-pod.yaml")
                continue
            got = pod.get("spec", {}).get("schedulerName", "")
            if got != expected:
                problems.append(f"{def_name}: generated schedulerName={got!r}, expected {expected!r}")

        assert not problems, "Generated workflow pod schedulerName mismatches:\n" + "\n".join(problems)

    def test_deployed_hook_configmap_matches_generated(
        self,
        upstream_dir: Path,
        enabled_modules: list[str],
        resolve_config,
    ) -> None:
        cluster_default = resolve_config("arc-runners.scheduler_name", "")
        _skip_unless_scheduler_configured(enabled_modules, cluster_default)

        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)
        defs = load_runner_defs(upstream_dir, enabled_arc)

        result = run_kubectl(["get", "configmaps", "-l", MODULE_LABEL_KEY, "-o", "json"], namespace=NAMESPACE)
        cms_by_name = {
            item["metadata"]["name"]: item
            for item in result.get("items", [])
            if item.get("metadata", {}).get("labels", {}).get(MODULE_LABEL_KEY) in enabled_arc
        }

        problems: list[str] = []
        for runner in defs:
            cm_name = f"arc-runner-hook-{_normalize_name(runner['name'])}"
            cm = cms_by_name.get(cm_name)
            if cm is None:
                continue
            expected = _effective_scheduler_name(runner, cluster_default)
            pod = _workflow_pod_from_cm_data(cm.get("data", {}) or {})
            if pod is None:
                problems.append(f"{cm_name}: no parseable job-pod.yaml in deployed ConfigMap")
                continue
            got = pod.get("spec", {}).get("schedulerName", "")
            if got != expected:
                problems.append(f"{cm_name}: deployed schedulerName={got!r}, expected {expected!r}")

        assert not problems, "Deployed hook ConfigMap schedulerName mismatches:\n" + "\n".join(problems)

    def test_listener_pods_carry_scheduler_env_var(
        self,
        all_pods: dict,
        upstream_dir: Path,
        enabled_modules: list[str],
        resolve_config,
    ) -> None:
        cluster_default = resolve_config("arc-runners.scheduler_name", "")
        _skip_unless_scheduler_configured(enabled_modules, cluster_default)

        enabled_arc = arc_runners_module_names(upstream_dir) & set(enabled_modules)
        defs_by_name = load_defs_by_name(upstream_dir, enabled_arc)
        runner_name_prefix = resolve_config("arc-runners.runner_name_prefix", "")

        listener_pods = filter_pods(all_pods, namespace=LISTENER_NAMESPACE, labels=LISTENER_LABELS)
        assert len(listener_pods) >= 1, (
            f"No listener pods found in '{LISTENER_NAMESPACE}' with labels {LISTENER_LABELS}"
        )

        problems: list[str] = []
        for pod in listener_pods:
            pod_name = pod["metadata"]["name"]
            containers = pod.get("spec", {}).get("containers", [])
            listener = next((c for c in containers if c.get("name") == LISTENER_CONTAINER_NAME), None)
            if listener is None:
                problems.append(f"{pod_name}: no '{LISTENER_CONTAINER_NAME}' container")
                continue

            def_name, runner = def_for_listener_pod(pod, defs_by_name, runner_name_prefix)
            if def_name is None or runner is None:
                continue

            expected = _effective_scheduler_name(runner, cluster_default)
            env_by_name = {e["name"]: e for e in listener.get("env", []) or []}
            entry = env_by_name.get(SCHEDULER_ENV_VAR)
            if entry is None:
                problems.append(f"{pod_name} (def={def_name}): missing env var {SCHEDULER_ENV_VAR!r}")
                continue
            got = entry.get("value", "") or ""
            if got != expected:
                problems.append(f"{pod_name} (def={def_name}): {SCHEDULER_ENV_VAR}={got!r}, expected {expected!r}")

        assert not problems, f"Listener {SCHEDULER_ENV_VAR} mismatches:\n" + "\n".join(problems)
