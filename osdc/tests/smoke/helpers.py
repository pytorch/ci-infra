"""CLI wrappers and filter utilities for OSDC smoke tests."""

from __future__ import annotations

import json
import subprocess
import time

DEFAULT_TIMEOUT = 60
READY_RETRIES = 3
READY_RETRY_DELAY = 10  # seconds


def run_kubectl(
    args: list[str], namespace: str | None = None, timeout: int = DEFAULT_TIMEOUT, *, json_output: bool = True
) -> dict | str:
    """Run kubectl, optionally parse JSON output."""
    cmd = ["kubectl"]
    if namespace:
        cmd.extend(["-n", namespace])
    cmd.extend(args)
    if json_output:
        cmd.extend(["-o", "json"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    if json_output:
        return json.loads(result.stdout)
    return result.stdout.strip()


def run_helm(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Run helm with -o json, return parsed output."""
    cmd = ["helm", *args, "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def run_aws(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run aws CLI with --output json, return parsed output."""
    cmd = ["aws", *args, "--output", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def filter_pods(all_pods: dict, namespace: str | None = None, labels: dict[str, str] | None = None) -> list[dict]:
    """Filter pods from batch-fetched pod list."""
    pods = all_pods.get("items", [])
    if namespace:
        pods = [p for p in pods if p["metadata"]["namespace"] == namespace]
    if labels:

        def _match(pod: dict) -> bool:
            pod_labels = pod.get("metadata", {}).get("labels", {})
            return all(pod_labels.get(k) == v for k, v in labels.items())

        pods = [p for p in pods if _match(p)]
    return pods


def filter_deployments(
    all_deployments: dict, namespace: str | None = None, name: str | None = None, name_contains: str | None = None
) -> list[dict]:
    """Filter deployments from batch-fetched list."""
    items = all_deployments.get("items", [])
    if namespace:
        items = [d for d in items if d["metadata"]["namespace"] == namespace]
    if name:
        items = [d for d in items if d["metadata"]["name"] == name]
    if name_contains:
        items = [d for d in items if name_contains in d["metadata"]["name"]]
    return items


def filter_daemonsets(
    all_daemonsets: dict, namespace: str | None = None, name: str | None = None, name_contains: str | None = None
) -> list[dict]:
    """Filter daemonsets from batch-fetched list."""
    items = all_daemonsets.get("items", [])
    if namespace:
        items = [d for d in items if d["metadata"]["namespace"] == namespace]
    if name:
        items = [d for d in items if d["metadata"]["name"] == name]
    if name_contains:
        items = [d for d in items if name_contains in d["metadata"]["name"]]
    return items


def filter_services(
    all_services: dict, namespace: str | None = None, name: str | None = None, name_contains: str | None = None
) -> list[dict]:
    """Filter services from batch-fetched list."""
    items = all_services.get("items", [])
    if namespace:
        items = [s for s in items if s["metadata"]["namespace"] == namespace]
    if name:
        items = [s for s in items if s["metadata"]["name"] == name]
    if name_contains:
        items = [s for s in items if name_contains in s["metadata"]["name"]]
    return items


def find_helm_release(all_releases: list[dict], name: str, namespace: str | None = None) -> dict | None:
    """Find a Helm release by name and optional namespace."""
    for rel in all_releases:
        if rel["name"] == name and (namespace is None or rel.get("namespace") == namespace):
            return rel
    return None


def assert_daemonset_ready(
    all_daemonsets: dict,
    namespace: str,
    name: str | None = None,
    *,
    name_contains: str | None = None,
    allow_zero: bool = False,
) -> None:
    """Assert a DaemonSet has all pods ready, retrying on transient mismatch.

    Uses the batch-fetched data first. If desired != ready, re-fetches the
    specific DaemonSet up to READY_RETRIES times (with READY_RETRY_DELAY
    between attempts) to tolerate node churn.

    Args:
        all_daemonsets: Batch-fetched DaemonSet data.
        namespace: Namespace to filter by.
        name: Exact DaemonSet name (mutually exclusive with name_contains).
        name_contains: Substring match on DaemonSet name.
        allow_zero: If True, 0/0 is acceptable (e.g. GPU plugin with no GPU nodes).
    """
    ds_list = filter_daemonsets(all_daemonsets, namespace=namespace, name=name, name_contains=name_contains)
    label = name or name_contains
    assert len(ds_list) >= 1, f"DaemonSet matching '{label}' not found in {namespace}"

    ds = ds_list[0]
    ds_name = ds["metadata"]["name"]
    desired = ds.get("status", {}).get("desiredNumberScheduled", 0)
    ready = ds.get("status", {}).get("numberReady", 0)

    if desired == ready:
        if not allow_zero:
            assert desired > 0, f"{ds_name} has 0 desired pods"
        return

    # Batch data is stale — retry with live fetches
    for attempt in range(READY_RETRIES):
        time.sleep(READY_RETRY_DELAY)
        fresh = run_kubectl(["get", f"daemonset/{ds_name}"], namespace=namespace)
        desired = fresh.get("status", {}).get("desiredNumberScheduled", 0)
        ready = fresh.get("status", {}).get("numberReady", 0)
        if desired == ready:
            if not allow_zero:
                assert desired > 0, f"{ds_name} has 0 desired pods"
            return

    assert ready == desired, f"{ds_name}: {ready}/{desired} pods ready (after {READY_RETRIES} retries)"


def assert_deployment_ready(
    all_deployments: dict,
    namespace: str,
    name: str,
) -> None:
    """Assert a Deployment has all replicas ready, retrying on transient mismatch.

    Same pattern as assert_daemonset_ready — uses batch data first, retries live.
    """
    deploys = filter_deployments(all_deployments, namespace=namespace, name=name)
    assert len(deploys) == 1, f"Deployment {name} not found in {namespace}"

    deploy = deploys[0]
    desired = deploy["spec"].get("replicas", 1)
    ready = deploy.get("status", {}).get("readyReplicas", 0)

    if desired == ready:
        return

    # Batch data is stale — retry with live fetches
    for attempt in range(READY_RETRIES):
        time.sleep(READY_RETRY_DELAY)
        fresh = run_kubectl(["get", f"deployment/{name}"], namespace=namespace)
        desired = fresh["spec"].get("replicas", 1)
        ready = fresh.get("status", {}).get("readyReplicas", 0)
        if desired == ready:
            return

    assert ready == desired, f"{name}: {ready}/{desired} replicas ready (after {READY_RETRIES} retries)"
