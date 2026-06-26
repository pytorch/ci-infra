"""K8s-resource readiness assertions — DaemonSets and Deployments."""

from __future__ import annotations

import time

from .cli import run_kubectl
from .filters import filter_daemonsets, filter_deployments
from .nodes import MIN_NODE_AGE_SECONDS, _count_unstable_nodes, _has_matching_nodes
from .retry import BACKOFF_ATTEMPTS, retry_with_backoff

# Re-exported for callers that still import the constant.
# Backed by the unified backoff primitive — total attempt count varies by
# environment (6 local, 8 CI) to match retry_with_backoff's schedule.
READY_RETRIES = BACKOFF_ATTEMPTS

# How long to keep polling for an in-progress Deployment rollout to finish
# before falling back to the standard retry. Kept as a small fixed interval
# because rollouts are deadline-based, not retry-count-based.
_ROLLOUT_POLL_INTERVAL = 15  # seconds

__all__ = [
    "READY_RETRIES",
    "_is_deployment_mid_rollout",
    "assert_daemonset_healthy",
    "assert_daemonset_ready",
    "assert_deployment_ready",
]


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
    specific DaemonSet via retry_with_backoff (BACKOFF_ATTEMPTS total tries
    with exponential backoff) to tolerate node churn.

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
        # Terminal state — no amount of retry will help. Assert and return.
        if not allow_zero:
            assert desired > 0, f"{ds_name} has 0 desired pods"
        return

    # Mismatch — retry on live re-fetches of the SPECIFIC DaemonSet.
    state: dict = {"desired": desired, "ready": ready}

    def check() -> None:
        d = state["desired"]
        r = state["ready"]
        assert r == d, f"{ds_name}: {r}/{d} pods ready (after {READY_RETRIES} retries)"
        if not allow_zero:
            assert d > 0, f"{ds_name} has 0 desired pods"

    def refresh() -> None:
        # Re-fetch ONLY the specific DaemonSet by name, not the whole batch.
        fresh = run_kubectl(["get", f"daemonset/{ds_name}"], namespace=namespace)
        state["desired"] = fresh.get("status", {}).get("desiredNumberScheduled", 0)
        state["ready"] = fresh.get("status", {}).get("numberReady", 0)

    retry_with_backoff(check, refresh=refresh)


def assert_daemonset_healthy(
    all_daemonsets: dict,
    all_nodes: dict,
    namespace: str,
    name: str | None = None,
    *,
    name_contains: str | None = None,
    allow_zero: bool = False,
    node_selector: dict[str, list[str]] | None = None,
    min_node_age: int = MIN_NODE_AGE_SECONDS,
) -> None:
    """Assert DaemonSet is healthy, tolerating mismatches from node churn.

    Passes if desired == ready, OR if the mismatch is fully explained by
    nodes that are new (< min_node_age seconds), NotReady, cordoned, or being deleted.

    This is resilient to concurrent node churn (compactor e2e, Karpenter
    autoscaling, spot interruptions, node recycling).

    Args:
        all_daemonsets: Batch-fetched DaemonSet data.
        all_nodes: Batch-fetched node data.
        namespace: Namespace to filter by.
        name: Exact DaemonSet name (mutually exclusive with name_contains).
        name_contains: Substring match on DaemonSet name.
        allow_zero: If True, 0/0 is acceptable (e.g. GPU plugin with no GPU nodes).
        node_selector: Label selector for the DaemonSet's target nodes, as
            ``{label_key: [value, ...]}``. When set and no nodes match, 0/0
            is accepted (the DaemonSet has no eligible nodes to schedule on).
        min_node_age: Minimum node age in seconds to consider stable.
    """
    ds_list = filter_daemonsets(all_daemonsets, namespace=namespace, name=name, name_contains=name_contains)
    label = name or name_contains
    assert len(ds_list) >= 1, f"DaemonSet matching '{label}' not found in {namespace}"

    ds = ds_list[0]
    ds_name = ds["metadata"]["name"]
    desired = ds.get("status", {}).get("desiredNumberScheduled", 0)
    ready = ds.get("status", {}).get("numberReady", 0)

    if desired == ready and (desired > 0 or allow_zero or not _has_matching_nodes(all_nodes, node_selector)):
        # Terminal — assertion already satisfied (or 0/0 is acceptable).
        return

    # Reaching here means either desired != ready, or desired == ready == 0
    # with eligible nodes — a transient post-deploy state where the controller
    # has not yet computed desired against the new eligible-node set. Both
    # cases drop into the retry loop.

    unstable = _count_unstable_nodes(all_nodes, min_node_age=min_node_age)
    if desired != ready and max(0, desired - ready) <= unstable:
        # Gap explained by node churn — terminal accept (no retry needed).
        if not allow_zero and ready == 0 and _has_matching_nodes(all_nodes, node_selector):
            assert desired > 0, f"{ds_name} has 0 ready pods (all {unstable} nodes unstable)"
        return

    # Mismatch unexplained by current node state — retry with live data.
    # Each refresh re-fetches BOTH the DaemonSet and the node list because
    # node churn is exactly what this assertion tolerates.
    state: dict = {
        "desired": desired,
        "ready": ready,
        "nodes": all_nodes,
        "unstable": unstable,
    }

    def check() -> None:
        d = state["desired"]
        r = state["ready"]
        nodes = state["nodes"]
        u = state["unstable"]
        if r == d:
            if not allow_zero and _has_matching_nodes(nodes, node_selector):
                assert d > 0, f"{ds_name} has 0 desired pods"
            return
        if max(0, d - r) <= u:
            if not allow_zero and r == 0 and _has_matching_nodes(nodes, node_selector):
                assert d > 0, f"{ds_name} has 0 ready pods (all {u} nodes unstable)"
            return
        raise AssertionError(
            f"{ds_name}: {r}/{d} pods ready, {u} unstable nodes "
            f"(after {READY_RETRIES} retries). "
            f"Mismatch exceeds unstable node count — this is a real failure."
        )

    def refresh() -> None:
        fresh_ds = run_kubectl(["get", f"daemonset/{ds_name}"], namespace=namespace)
        fresh_nodes = run_kubectl(["get", "nodes"])
        state["desired"] = fresh_ds.get("status", {}).get("desiredNumberScheduled", 0)
        state["ready"] = fresh_ds.get("status", {}).get("numberReady", 0)
        state["nodes"] = fresh_nodes
        state["unstable"] = _count_unstable_nodes(fresh_nodes, min_node_age=min_node_age)

    retry_with_backoff(check, refresh=refresh)


def _is_deployment_mid_rollout(deploy: dict) -> bool:
    """Check if a Deployment is in the middle of a rollout.

    A rollout is in progress when:
    - observedGeneration < metadata.generation (controller hasn't processed the update yet), OR
    - The Progressing condition is True but reason is NOT 'NewReplicaSetAvailable'
      (a new ReplicaSet is being scaled up)
    """
    meta_gen = deploy.get("metadata", {}).get("generation", 0)
    observed_gen = deploy.get("status", {}).get("observedGeneration", 0)
    if observed_gen < meta_gen:
        return True

    conditions = {c["type"]: c for c in deploy.get("status", {}).get("conditions", [])}
    progressing = conditions.get("Progressing", {})
    if progressing.get("status") == "True" and progressing.get("reason") != "NewReplicaSetAvailable":
        return True

    return False


def assert_deployment_ready(
    all_deployments: dict,
    namespace: str,
    name: str,
    *,
    rollout_timeout: int = 90,
) -> None:
    """Assert a Deployment has all replicas ready, tolerating active rollouts.

    If the deployment is mid-rollout (e.g. triggered by a parallel e2e test),
    waits for the rollout to complete before checking readiness. Fails if the
    rollout doesn't complete within rollout_timeout seconds (stuck rollout).

    Args:
        all_deployments: Batch-fetched Deployment data.
        namespace: Namespace to filter by.
        name: Deployment name.
        rollout_timeout: Max seconds to wait for an active rollout to finish.
            Node-compactor rollouts (Recreate + 30s readiness probe) should
            complete in under 60s; 90s gives comfortable margin.
    """
    deploys = filter_deployments(all_deployments, namespace=namespace, name=name)
    assert len(deploys) == 1, f"Deployment {name} not found in {namespace}"

    deploy = deploys[0]
    desired = deploy["spec"].get("replicas", 1)
    ready = deploy.get("status", {}).get("readyReplicas", 0)

    if desired == ready:
        return

    # Phase 2: deadline-based polling while a rollout is in progress.
    # Intentionally NOT retry_with_backoff — this is bounded by wall clock,
    # not attempt count, with a short fixed poll interval.
    fresh = deploy
    if _is_deployment_mid_rollout(deploy):
        deadline = time.time() + rollout_timeout
        rollout_completed = False
        while time.time() < deadline:
            time.sleep(_ROLLOUT_POLL_INTERVAL)
            fresh = run_kubectl(["get", f"deployment/{name}"], namespace=namespace)
            desired = fresh["spec"].get("replicas", 1)
            ready = fresh.get("status", {}).get("readyReplicas", 0)
            if desired == ready:
                return
            if not _is_deployment_mid_rollout(fresh):
                # Rollout finished but still not ready — fall through to standard retry.
                rollout_completed = True
                break
        if not rollout_completed:
            raise AssertionError(
                f"{name}: rollout still in progress after {rollout_timeout}s "
                f"({ready}/{desired} replicas ready). Rollout may be stuck."
            )

    # Phase 3: standard fallback retry on the latest known deployment state.
    state: dict = {"desired": desired, "ready": ready}

    def check() -> None:
        d = state["desired"]
        r = state["ready"]
        assert r == d, f"{name}: {r}/{d} replicas ready (after {READY_RETRIES} retries)"

    def refresh() -> None:
        live = run_kubectl(["get", f"deployment/{name}"], namespace=namespace)
        state["desired"] = live["spec"].get("replicas", 1)
        state["ready"] = live.get("status", {}).get("readyReplicas", 0)

    retry_with_backoff(check, refresh=refresh)
