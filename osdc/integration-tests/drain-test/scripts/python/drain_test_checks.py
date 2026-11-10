"""Fail-closed assertions shared by the drain/undrain slow test."""

from __future__ import annotations

import math
import re
import sys
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "modules" / "arc-runners" / "scripts" / "python"))

from runner_max_map import MAX_INT32

CAPACITY_METRIC = "gha_capacity_advertised_max_runners"
SCALE_SET_LABEL = "actions.github.com/scale-set-name"


def runner_limits(items: list[dict]) -> dict[str, int]:
    """Read effective limits; an omitted maxRunners means unlimited in ARC."""
    result = {}
    for item in items:
        name = item["metadata"]["name"]
        value = item["spec"].get("maxRunners")
        if value is None:
            value = MAX_INT32
        if name in result or type(value) is not int or value < 0:
            raise ValueError(f"Invalid maxRunners snapshot for {name}: {value!r}")
        result[name] = value
    if not result:
        raise ValueError("No AutoscalingRunnerSets found; this is not a successful drain")
    return result


def require_limits(actual: dict[str, int], expected: dict[str, int]) -> None:
    """Require the complete configured map, including region-excluded zero limits."""
    if not expected or actual != expected:
        raise ValueError(f"Runner limits differ: actual={actual}, expected={expected}")


def probe_scale_set(items: list[dict], desired: dict[str, int], *, label: str, cluster: str) -> str:
    """Require an enabled probe scale set in the target cluster."""
    for item in items:
        name = item["metadata"]["name"]
        if item["spec"].get("runnerScaleSetName") == label and desired.get(name, 0) > 0:
            return name
    raise ValueError(f"No enabled scale set for {label} in {cluster}")


def listener_pods(items: list[dict], expected: set[str]) -> dict[str, tuple[str, int]]:
    """Require a ready listener with a metrics port for every tested scale set."""
    result = {}
    for pod in items:
        metadata = pod["metadata"]
        scale_set = metadata.get("labels", {}).get(SCALE_SET_LABEL)
        if scale_set not in expected or metadata.get("deletionTimestamp"):
            continue
        if not any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in pod.get("status", {}).get("conditions", [])
        ):
            continue
        ports = [
            port["containerPort"]
            for container in pod["spec"]["containers"]
            for port in container.get("ports", [])
            if port.get("name") == "metrics"
        ]
        if len(ports) != 1 or scale_set in result:
            raise ValueError(f"Ambiguous listener/metrics port for {scale_set}")
        result[scale_set] = (metadata["name"], ports[0])
    if not expected or set(result) != expected:
        raise ValueError(f"Missing ready listeners: {sorted(expected - set(result))}")
    return result


def advertised_capacity(metrics: str, scale_set: str, *, metric_names: set[str] | None = None) -> float:
    """Missing/NaN metrics must not look like zero advertised capacity.

    A listener may advertise the same capacity under both the AutoscalingRunnerSet
    and the EphemeralRunnerSet labels; accept the largest finite, non-negative
    value among the matched series instead of requiring exactly one sample.

    When metric_names is provided, only series whose name label appears in the
    set are considered. When it is None, all capacity metric series are accepted;
    the caller is responsible for ensuring the metrics come from the correct
    listener pod (e.g. by selecting pods via scale-set label).
    """
    by_name: dict[str, float] = {}
    for family in text_string_to_metric_families(metrics):
        for sample in family.samples:
            if sample.name == CAPACITY_METRIC and (metric_names is None or sample.labels.get("name") in metric_names):
                name = sample.labels.get("name")
                if name in by_name:
                    raise ValueError(f"Duplicate {CAPACITY_METRIC} sample for {name} in {scale_set}")
                by_name[name] = sample.value
    if not by_name or not all(math.isfinite(v) and v >= 0 for v in by_name.values()):
        raise ValueError(f"Missing or invalid {CAPACITY_METRIC} for {scale_set}: {by_name}")
    return max(by_name.values())


def probe_jobs(jobs: list[dict], phase: str, count: int) -> list[dict]:
    """Match named jobs, never treat a missing job as queued or successful."""
    result = [job for job in jobs if job["name"].startswith(f"drain-{phase}")]
    if len(result) != count:
        raise ValueError(f"Expected {count} {phase} jobs, got {len(result)}")
    return result


def require_queued(jobs: list[dict]) -> None:
    """Reject jobs that ran even briefly, including already completed jobs."""
    if not jobs or any(job["status"] != "queued" or job.get("runner_id") or 0 for job in jobs):
        raise ValueError(f"Jobs escaped the drained cluster: {jobs}")


def require_success(jobs: list[dict], group: str) -> None:
    """Completion alone is insufficient; verify success and runner group identity."""
    if not jobs or any(
        job["status"] != "completed" or job.get("conclusion") != "success" or job.get("runner_group_name") != group
        for job in jobs
    ):
        raise ValueError(f"Jobs did not succeed on {group}: {jobs}")


def held_probe_number(job: dict) -> int:
    """Match the explicit matrix job name before constructing a release marker."""
    match = re.fullmatch(r"drain-held \(([123])\)", job["name"])
    if match is None:
        raise RuntimeError(f"Unexpected held probe name: {job['name']}")
    return int(match[1])


def require_probe_placement(
    job: dict, pods: list[dict], nodes: list[dict], stale_uids: set[str], *, scale_set: str
) -> dict:
    """Require both probe pods on non-stale nodes; missing evidence is retryable."""
    runner_name = job.get("runner_name")
    if not runner_name:
        raise ValueError(f"Missing runner name for job {job['id']}")
    runners = [pod for pod in pods if pod["metadata"]["name"] == runner_name]
    if len(runners) != 1:
        raise ValueError(f"Missing or ambiguous runner pod for job {job['id']}: {runner_name}")
    runner = runners[0]
    labels = runner["metadata"].get("labels", {})
    if labels.get(SCALE_SET_LABEL) != scale_set or labels.get("app.kubernetes.io/component") != "runner":
        raise RuntimeError(f"Runner pod {runner_name} does not belong to probe scale set {scale_set}")
    # Hooks label both job and step pods; require the actual job container.
    workflows = [
        pod
        for pod in pods
        if pod["metadata"].get("labels", {}).get("runner-pod") == runner_name
        and any(container.get("name") == "job" for container in pod["spec"].get("containers", []))
    ]
    if len(workflows) != 1:
        raise ValueError(f"Missing or ambiguous workflow pod for runner {runner_name}")
    evidence = {}
    for role, pod in (("runner", runner), ("workflow", workflows[0])):
        metadata = pod["metadata"]
        node_name = pod["spec"].get("nodeName")
        matches = [node for node in nodes if node["metadata"]["name"] == node_name]
        if (
            not metadata.get("uid")
            or metadata.get("deletionTimestamp")
            or pod.get("status", {}).get("phase") != "Running"
            or not node_name
            or len(matches) != 1
            or not matches[0]["metadata"].get("uid")
        ):
            raise ValueError(f"Incomplete {role} pod/node evidence for job {job['id']}")
        node_uid = matches[0]["metadata"]["uid"]
        if node_uid in stale_uids:
            raise RuntimeError(f"Probe job {job['id']} {role} pod {metadata['name']} reused stale node {node_uid}")
        evidence[role] = {
            "pod": metadata["name"],
            "pod_uid": metadata["uid"],
            "node": node_name,
            "node_uid": node_uid,
        }
    return evidence
