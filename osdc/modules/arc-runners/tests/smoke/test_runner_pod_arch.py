"""Smoke tests: each runner pod runs on the architecture of its job.

GitHub reports the runner agent's architecture as ``runner.arch`` / ``RUNNER_ARCH``.
The agent lives in the runner pod, while the job's steps run in a separate workflow
pod on the node its label picked. Runner pods therefore go on the runner-pod pool of
the job's architecture (``c7i-runner`` for x86, ``arm64-runner`` for arm64), so those
values stay true for the job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fleet_naming import derive_runner_pod_node_fleet
from instance_specs import INSTANCE_SPECS
from runner_defs import GeneratedScaleSet, load_defs_by_name

pytestmark = [pytest.mark.live]

NAMESPACE = "arc-runners"
ARCH_LABEL = "kubernetes.io/arch"


def _expected_arch(scale_set: GeneratedScaleSet, defs_by_name: dict[str, dict]) -> str:
    return INSTANCE_SPECS[defs_by_name[scale_set.def_name]["instance_type"]]["arch"]


def _listener_env(scale_set: GeneratedScaleSet) -> dict[str, str]:
    containers = scale_set.values["listenerTemplate"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in containers[0].get("env", [])}


def _node_arch(all_nodes: dict) -> dict[str, str]:
    return {n["metadata"]["name"]: n["metadata"].get("labels", {}).get(ARCH_LABEL, "") for n in all_nodes["items"]}


class TestRunnerPodArch:
    def test_generated_runner_pods_target_their_arch_pool(
        self, generated_scale_sets: list[GeneratedScaleSet], upstream_dir: Path
    ) -> None:
        """Every scale set's runner pods and runner placeholders target the pool of its arch."""
        defs_by_name = load_defs_by_name(upstream_dir)
        wrong = []
        for ss in generated_scale_sets:
            expected = derive_runner_pod_node_fleet(_expected_arch(ss, defs_by_name))
            pod_fleet = ss.values["template"]["spec"]["nodeSelector"].get("node-fleet")
            placeholder_fleet = _listener_env(ss).get("CAPACITY_AWARE_RUNNER_NODE_FLEET")
            if pod_fleet != expected or placeholder_fleet != expected:
                wrong.append(f"{ss.scale_set_label}: pod={pod_fleet} placeholder={placeholder_fleet} want={expected}")
        assert not wrong, "Scale sets on the wrong runner-pod pool:\n  " + "\n  ".join(wrong)

    def test_live_runner_pod_arch_matches_its_workflow_pod(self, all_pods: dict, all_nodes: dict) -> None:
        """For every running job, the runner pod's node has the workflow pod's node architecture."""
        arch_of = _node_arch(all_nodes)
        pods = [p for p in all_pods["items"] if p["metadata"].get("namespace") == NAMESPACE]
        node_of = {p["metadata"]["name"]: p.get("spec", {}).get("nodeName") for p in pods}
        mismatched = []
        checked = 0
        for p in pods:
            runner_pod = p["metadata"].get("labels", {}).get("runner-pod")
            workflow_node = p.get("spec", {}).get("nodeName")
            runner_node = node_of.get(runner_pod or "")
            if not (runner_pod and workflow_node and runner_node):
                continue
            checked += 1
            if arch_of.get(runner_node) != arch_of.get(workflow_node):
                mismatched.append(
                    f"{runner_pod}: runner on {arch_of.get(runner_node)}, job on {arch_of.get(workflow_node)}"
                )
        if not checked:
            pytest.skip("no scheduled runner/workflow pod pairs to check")
        assert not mismatched, f"{len(mismatched)}/{checked} jobs report the wrong runner.arch:\n  " + "\n  ".join(
            mismatched[:20]
        )

    def test_live_runner_placeholders_on_their_arch(
        self,
        all_pods: dict,
        all_nodes: dict,
        generated_scale_sets: list[GeneratedScaleSet],
        upstream_dir: Path,
    ) -> None:
        """Runner placeholders reserve room on the pool their scale set's runner pods will use."""
        arch_of = _node_arch(all_nodes)
        defs_by_name = load_defs_by_name(upstream_dir)
        expected_by_label = {ss.scale_set_label: _expected_arch(ss, defs_by_name) for ss in generated_scale_sets}
        wrong = []
        checked = 0
        for p in all_pods["items"]:
            labels = p["metadata"].get("labels", {})
            if p["metadata"].get("namespace") != NAMESPACE or labels.get("capacity.actions.github.com/role") != (
                "placeholder-runner"
            ):
                continue
            node = p.get("spec", {}).get("nodeName")
            expected = expected_by_label.get(labels.get("actions.github.com/scale-set-name", ""))
            if not node or expected is None:
                continue
            checked += 1
            if arch_of.get(node) != expected:
                wrong.append(f"{p['metadata']['name']}: on {arch_of.get(node)}, want {expected}")
        if not checked:
            pytest.skip("no scheduled runner placeholders to check")
        assert not wrong, f"{len(wrong)}/{checked} runner placeholders on the wrong arch:\n  " + "\n  ".join(wrong[:20])
