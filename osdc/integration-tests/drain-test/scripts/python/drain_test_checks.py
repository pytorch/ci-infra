"""Fail-closed assertions shared by the drain/undrain slow test."""

from __future__ import annotations

import math

from prometheus_client.parser import text_string_to_metric_families

CLUSTER = "meta-staging-aws-uw1"
CAPACITY_METRIC = "gha_capacity_advertised_max_runners"
SCALE_SET_LABEL = "actions.github.com/scale-set-name"
PROBE_LABEL = "c-mt-l-x86iamx-8-16"


def runner_limits(items: list[dict]) -> dict[str, int]:
    """Read effective limits; an omitted maxRunners means unlimited in ARC."""
    result = {}
    for item in items:
        name = item["metadata"]["name"]
        value = item["spec"].get("maxRunners")
        if value is None:
            value = 2**31 - 1  # Same default as runner_max_map.py and ARC.
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


def probe_scale_set(
    items: list[dict], desired: dict[str, int], *, label: str = PROBE_LABEL, cluster: str = CLUSTER
) -> str:
    """Use a small CPU label served in uw1, never a region-excluded scale set."""
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
    """Missing/NaN metrics must not look like zero advertised capacity."""
    values = [
        sample.value
        for family in text_string_to_metric_families(metrics)
        for sample in family.samples
        if sample.name == CAPACITY_METRIC and sample.labels.get("name") in (metric_names or {scale_set})
    ]
    if len(values) != 1 or not math.isfinite(values[0]) or values[0] < 0:
        raise ValueError(f"Missing or invalid {CAPACITY_METRIC} for {scale_set}: {values}")
    return values[0]


def probe_jobs(jobs: list[dict], phase: str, count: int) -> list[dict]:
    """Match named jobs, never treat a missing job as queued or successful."""
    result = [job for job in jobs if job["name"].startswith(f"drain-{phase}")]
    if len(result) != count:
        raise ValueError(f"Expected {count} {phase} jobs, got {len(result)}")
    return result


def require_queued(jobs: list[dict]) -> None:
    """Reject jobs that ran even briefly, including already completed jobs."""
    if not jobs or any(job["status"] != "queued" or job.get("runner_id", 0) for job in jobs):
        raise ValueError(f"Jobs escaped the drained cluster: {jobs}")


def require_success(jobs: list[dict], group: str) -> None:
    """Completion alone is insufficient; verify success and runner group identity."""
    if not jobs or any(
        job["status"] != "completed" or job.get("conclusion") != "success" or job.get("runner_group_name") != group
        for job in jobs
    ):
        raise ValueError(f"Jobs did not succeed on {group}: {jobs}")
