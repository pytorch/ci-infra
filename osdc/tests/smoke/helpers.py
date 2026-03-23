"""CLI wrappers and filter utilities for OSDC smoke tests."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 60
READY_RETRIES = 6
READY_RETRY_DELAY = 15  # seconds
MIN_NODE_AGE_SECONDS = 120  # Nodes must be Ready for 2+ min to count as stable


def _proxy_bypass_env() -> dict[str, str]:
    """Return an env dict that bypasses corporate proxy for AWS API calls.

    The Meta corporate proxy intercepts HTTPS connections, which causes
    kubectl/helm to fail with 'Unauthorized' when talking to EKS, and
    AWS CLI calls to fail for services like ECR, IAM, SQS, EventBridge.
    Using '.amazonaws.com' covers all AWS service endpoints at once.
    """
    env = os.environ.copy()
    aws_suffix = ".amazonaws.com"
    for key in ("NO_PROXY", "no_proxy"):
        current = env.get(key, "")
        if aws_suffix not in current:
            env[key] = f"{current},{aws_suffix}" if current else aws_suffix
    return env


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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=_proxy_bypass_env())
    if json_output:
        return json.loads(result.stdout)
    return result.stdout.strip()


def run_helm(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Run helm with -o json, return parsed output."""
    cmd = ["helm", *args, "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=_proxy_bypass_env())
    return json.loads(result.stdout)


def run_aws(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run aws CLI with --output json, return parsed output."""
    cmd = ["aws", *args, "--output", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=_proxy_bypass_env())
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
    for _attempt in range(READY_RETRIES):
        time.sleep(READY_RETRY_DELAY)
        fresh = run_kubectl(["get", f"daemonset/{ds_name}"], namespace=namespace)
        desired = fresh.get("status", {}).get("desiredNumberScheduled", 0)
        ready = fresh.get("status", {}).get("numberReady", 0)
        if desired == ready:
            if not allow_zero:
                assert desired > 0, f"{ds_name} has 0 desired pods"
            return

    assert ready == desired, f"{ds_name}: {ready}/{desired} pods ready (after {READY_RETRIES} retries)"


def _parse_k8s_timestamp(ts: str) -> float:
    """Parse Kubernetes ISO 8601 timestamp to Unix epoch seconds."""
    from datetime import datetime

    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _is_node_unstable(node: dict) -> bool:
    """Check if a single node is unstable (new, NotReady, or being deleted)."""
    meta = node.get("metadata", {})
    if meta.get("deletionTimestamp"):
        return True
    conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
    if conditions.get("Ready") != "True":
        return True
    created = meta.get("creationTimestamp", "")
    if created:
        created_ts = _parse_k8s_timestamp(created)
        if (time.time() - created_ts) < MIN_NODE_AGE_SECONDS:
            return True
    return False


def _count_unstable_nodes(all_nodes: dict) -> int:
    """Count nodes that are new, NotReady, or being deleted.

    A node is "unstable" if any of:
    - It has a deletionTimestamp (being deleted)
    - Its Ready condition is not True
    - It was created less than MIN_NODE_AGE_SECONDS ago
    """
    return sum(1 for node in all_nodes.get("items", []) if _is_node_unstable(node))


def get_unstable_node_names(all_nodes: dict) -> set[str]:
    """Return names of nodes that are new, NotReady, or being deleted."""
    return {
        node["metadata"]["name"]
        for node in all_nodes.get("items", [])
        if _is_node_unstable(node)
    }


def assert_daemonset_healthy(
    all_daemonsets: dict,
    all_nodes: dict,
    namespace: str,
    name: str | None = None,
    *,
    name_contains: str | None = None,
    allow_zero: bool = False,
) -> None:
    """Assert DaemonSet is healthy, tolerating mismatches from node churn.

    Passes if desired == ready, OR if the mismatch is fully explained by
    nodes that are new (< MIN_NODE_AGE_SECONDS), NotReady, or being deleted.

    This is resilient to concurrent node churn (compactor e2e, Karpenter
    autoscaling, spot interruptions, node recycling).

    Args:
        all_daemonsets: Batch-fetched DaemonSet data.
        all_nodes: Batch-fetched node data.
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

    # Check if mismatch is explained by unstable nodes
    unstable = _count_unstable_nodes(all_nodes)
    if max(0, desired - ready) <= unstable:
        if not allow_zero and ready == 0:
            assert desired > 0, f"{ds_name} has 0 ready pods (all {unstable} nodes unstable)"
        return

    # Batch data may be stale — retry with live fetches
    for _attempt in range(READY_RETRIES):
        time.sleep(READY_RETRY_DELAY)
        fresh_ds = run_kubectl(["get", f"daemonset/{ds_name}"], namespace=namespace)
        fresh_nodes = run_kubectl(["get", "nodes"])
        desired = fresh_ds.get("status", {}).get("desiredNumberScheduled", 0)
        ready = fresh_ds.get("status", {}).get("numberReady", 0)
        if desired == ready:
            if not allow_zero:
                assert desired > 0, f"{ds_name} has 0 desired pods"
            return
        unstable = _count_unstable_nodes(fresh_nodes)
        if max(0, desired - ready) <= unstable:
            if not allow_zero and ready == 0:
                assert desired > 0, f"{ds_name} has 0 ready pods (all {unstable} nodes unstable)"
            return

    raise AssertionError(
        f"{ds_name}: {ready}/{desired} pods ready, {unstable} unstable nodes "
        f"(after {READY_RETRIES} retries). "
        f"Mismatch exceeds unstable node count — this is a real failure."
    )


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
    for _attempt in range(READY_RETRIES):
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

    Write: https://logs-prod-021.grafana.net/loki/api/v1/push
    Read:  https://logs-prod-021.grafana.net/loki/api/v1/query_range
    """
    base = write_url.rstrip("/")
    if base.endswith("/push"):
        base = base[: -len("/push")]
    return f"{base}/query_range"


def fetch_grafana_cloud_credentials(
    namespace: str,
    username_key: str,
    password_key: str,
    secret_name: str = "grafana-cloud-credentials",
) -> tuple[str, str] | None:
    """Fetch Grafana Cloud credentials from a Kubernetes secret.

    Returns (username, password) or None if the secret doesn't exist
    or cannot be decoded.
    """
    try:
        secret = run_kubectl(["get", "secret", secret_name], namespace=namespace)
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
    except Exception as exc:
        query_mimir.last_error = str(exc)
        return None

query_mimir.last_error = ""


def query_loki(url: str, logql: str, username: str, password: str, timeout: int = 30) -> dict | None:
    """Query Grafana Cloud Loki (LogQL query_range). Returns None on error."""
    now = int(time.time())
    params = urllib.parse.urlencode({
        "query": logql,
        "start": str(now - 3600),
        "end": str(now),
        "limit": "1",
        "direction": "backward",
    })
    full_url = f"{url}?{params}"
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    req = urllib.request.Request(full_url, headers={"Authorization": f"Basic {auth}"})
    try:
        with _urlopen_no_proxy(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        query_loki.last_error = str(exc)
        return None

query_loki.last_error = ""


# ---------------------------------------------------------------------------
# Per-target / per-source remote verification helpers
# ---------------------------------------------------------------------------

REMOTE_RETRIES = 5
REMOTE_RETRY_DELAY = 15


def assert_metric_fresh_in_mimir(
    read_url: str,
    promql: str,
    username: str,
    password: str,
    max_staleness: int = 600,
    description: str = "",
) -> None:
    """Query Mimir for a PromQL expression and assert fresh data exists.

    Retries up to REMOTE_RETRIES times on network errors OR staleness.
    This handles transient gaps from pipeline restarts (e.g. Alloy pod
    churn) — if data is stale on the first check, the pipeline may still
    be catching up.

    Raises AssertionError if metric is still missing or stale after all retries.
    Raises pytest.skip if all retries fail with network errors.
    """
    import pytest

    label = description or promql
    last_err = ""
    last_age: float | None = None

    for attempt in range(REMOTE_RETRIES):
        if attempt > 0:
            time.sleep(REMOTE_RETRY_DELAY)

        result = query_mimir(read_url, promql, username, password)
        if result is None:
            last_err = getattr(query_mimir, "last_error", "unknown")
            continue

        # Got a response — check it
        status = result.get("status", "")
        assert status == "success", f"[{label}] Mimir query returned status '{status}'"

        results = result.get("data", {}).get("result", [])
        if len(results) == 0:
            last_err = "no metric series found"
            continue

        newest_ts = max(float(r["value"][0]) for r in results if r.get("value"))
        last_age = time.time() - newest_ts
        if last_age < max_staleness:
            return
        # Stale — retry in case the pipeline is catching up
        last_err = f"stale ({last_age:.0f}s)"

    # All retries exhausted — determine failure mode
    if last_age is not None:
        assert last_age < max_staleness, (
            f"[{label}] Metric is stale after {REMOTE_RETRIES} attempts: "
            f"newest sample is {last_age:.0f}s old (threshold: {max_staleness}s)"
        )
    if "no metric series found" in last_err:
        raise AssertionError(f"[{label}] No metric series found after {REMOTE_RETRIES} attempts")
    pytest.skip(f"[{label}] Mimir unreachable after {REMOTE_RETRIES} attempts: {last_err}")


def assert_logs_fresh_in_loki(
    read_url: str,
    logql: str,
    username: str,
    password: str,
    max_staleness: int = 600,
    description: str = "",
) -> None:
    """Query Loki for a LogQL expression and assert fresh log streams exist.

    Retries up to REMOTE_RETRIES times on network errors OR staleness.
    This handles transient gaps from pipeline restarts (e.g. Alloy DaemonSet
    pod churn) — if data is stale on the first check, the pipeline may still
    be catching up.

    Raises AssertionError if logs are still missing or stale after all retries.
    Raises pytest.skip if all retries fail with network errors.
    """
    import pytest

    label = description or logql
    last_err = ""
    last_age: float | None = None

    for attempt in range(REMOTE_RETRIES):
        if attempt > 0:
            time.sleep(REMOTE_RETRY_DELAY)

        result = query_loki(read_url, logql, username, password)
        if result is None:
            last_err = getattr(query_loki, "last_error", "unknown")
            continue

        # Got a response — check it
        status = result.get("status", "")
        assert status == "success", f"[{label}] Loki query returned status '{status}'"

        streams = result.get("data", {}).get("result", [])
        if len(streams) == 0:
            last_err = "no log streams found"
            continue

        # Loki query_range returns streams with "values": [[nanosecond_ts, line], ...]
        newest_ns = 0
        for stream in streams:
            for ts_ns, _ in stream.get("values", []):
                newest_ns = max(newest_ns, int(ts_ns))

        if newest_ns > 0:
            last_age = time.time() - (newest_ns / 1e9)
            if last_age < max_staleness:
                return
            # Stale — retry in case the pipeline is catching up
            last_err = f"stale ({last_age:.0f}s)"
        else:
            last_err = "no timestamps in log streams"
            continue

    # All retries exhausted — determine failure mode
    if last_age is not None:
        assert last_age < max_staleness, (
            f"[{label}] Logs are stale after {REMOTE_RETRIES} attempts: "
            f"newest entry is {last_age:.0f}s old (threshold: {max_staleness}s)"
        )
    if "no log streams found" in last_err:
        raise AssertionError(f"[{label}] No log streams found after {REMOTE_RETRIES} attempts")
    pytest.skip(f"[{label}] Loki unreachable after {REMOTE_RETRIES} attempts: {last_err}")
