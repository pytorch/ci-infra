"""CLI wrappers and filter utilities for OSDC smoke tests."""

from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.parse
import urllib.request

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


# ---------------------------------------------------------------------------
# Grafana Cloud remote query helpers
# ---------------------------------------------------------------------------


def mimir_read_url(write_url: str) -> str:
    """Derive Mimir read endpoint from the write URL.

    Write: https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push
    Read:  https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/api/v1/query
    """
    base = write_url.rstrip("/")
    if base.endswith("/push"):
        base = base[: -len("/push")]
    return f"{base}/api/v1/query"


def loki_read_url(write_url: str) -> str:
    """Derive Loki read endpoint from the write URL.

    Write: https://logs-prod-us-central1.grafana.net/loki/api/v1/push
    Read:  https://logs-prod-us-central1.grafana.net/loki/api/v1/query_range
    """
    base = write_url.rstrip("/")
    if base.endswith("/push"):
        base = base[: -len("/push")]
    return f"{base}/query_range"


def fetch_grafana_cloud_credentials(
    namespace: str, username_key: str, password_key: str
) -> tuple[str, str] | None:
    """Fetch Grafana Cloud credentials from a Kubernetes secret.

    Returns (username, password) or None if the secret doesn't exist
    or cannot be decoded.
    """
    try:
        secret = run_kubectl(["get", "secret", "grafana-cloud-credentials"], namespace=namespace)
        data = secret.get("data", {})
        if username_key not in data or password_key not in data:
            return None
        username = base64.b64decode(data[username_key]).decode()
        password = base64.b64decode(data[password_key]).decode()
        return (username, password)
    except Exception:
        return None


def _urlopen_no_proxy(req: urllib.request.Request, timeout: int = 30):
    """Open a URL request bypassing any configured HTTP(S) proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def query_mimir(url: str, promql: str, username: str, password: str, timeout: int = 30) -> dict | None:
    """Query Grafana Cloud Mimir (Prometheus-compatible API). Returns None on error."""
    full_url = f"{url}?query={urllib.parse.quote(promql)}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(full_url, headers={"Authorization": f"Basic {auth}"})
    try:
        with _urlopen_no_proxy(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def query_loki(url: str, logql: str, username: str, password: str, timeout: int = 30) -> dict | None:
    """Query Grafana Cloud Loki (LogQL query_range). Returns None on error."""
    now = int(time.time())
    params = urllib.parse.urlencode({
        "query": logql,
        "start": str(now - 3600),
        "end": str(now),
        "limit": "1",
    })
    full_url = f"{url}?{params}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(full_url, headers={"Authorization": f"Basic {auth}"})
    try:
        with _urlopen_no_proxy(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None
