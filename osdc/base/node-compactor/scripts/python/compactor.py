#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["lightkube~=0.15.5"]
# ///
"""Node Compactor Controller.

Proactively taints underutilized Karpenter-managed nodes with NoSchedule
so new pods land on denser nodes. Existing pods finish naturally. When a
tainted node becomes empty, Karpenter's WhenEmpty policy handles deletion.

This achieves cost savings (fewer nodes) without evicting running CI jobs.

Managed NodePools are discovered by the label:
    osdc.io/node-compactor: "true"

Configuration via environment variables -- see models.DEFAULTS.
"""

import logging
import pathlib
import signal
import sys
import time

import metrics as m
from discovery import build_node_states, discover_managed_nodes
from lightkube import ApiError, Client
from models import Config
from packing import compute_taints
from prometheus_client import start_http_server
from taints import apply_taint, check_pending_pods, cleanup_stale_taints, remove_taint

log = logging.getLogger("compactor")


# ============================================================================
# Reconciliation loop
# ============================================================================


def reconcile(client: Client, cfg: Config, taint_times: dict[str, float]) -> None:
    """Single reconciliation cycle.

    Args:
        client: Kubernetes API client.
        cfg: Compactor configuration.
        taint_times: Mutable dict tracking when each node was last tainted.
            Updated in-place when nodes are tainted.
    """
    # Touch healthcheck file at the start of every cycle so the liveness
    # probe passes even when there are no managed nodes (controller is
    # healthy, just idle).
    pathlib.Path("/tmp/healthy").touch()

    managed_names = discover_managed_nodes(client, cfg)
    if not managed_names:
        log.debug("No managed nodes found (no NodePools with label %s)", cfg.nodepool_label)
        m.refresh_gauge(m.managed_nodes, {})
        m.refresh_gauge(m.node_utilization_ratio, {})
        m.refresh_gauge(m.workload_pods, {})
        m.refresh_gauge(m.tainted_nodes, {})
        return

    # Instrumentation point 1: managed nodes per nodepool
    pool_node_counts: dict[str, int] = {}
    for node_name, pool_name in managed_names.items():
        pool_node_counts[pool_name] = pool_node_counts.get(pool_name, 0) + 1
    m.refresh_gauge(m.managed_nodes, {(pool_name,): count for pool_name, count in pool_node_counts.items()})

    node_states, pending_pods = build_node_states(client, cfg, managed_names)
    if not node_states:
        log.debug("No node states built")
        m.refresh_gauge(m.node_utilization_ratio, {})
        m.refresh_gauge(m.workload_pods, {})
        m.refresh_gauge(m.tainted_nodes, {})
        return

    total_pods = sum(ns.workload_pod_count for ns in node_states.values())
    total_nodes = len(node_states)
    tainted = sum(1 for ns in node_states.values() if ns.is_tainted)
    log.info(
        "Reconciling: %d nodes (%d tainted), %d workload pods",
        total_nodes,
        tainted,
        total_pods,
    )

    # Instrumentation point 2: workload pods and utilization per nodepool/node
    pool_pod_counts: dict[str, int] = {}
    utilization: dict[tuple[str, ...], float] = {}
    for node_name, ns in node_states.items():
        pool = managed_names.get(node_name, "unknown")
        pool_pod_counts[pool] = pool_pod_counts.get(pool, 0) + ns.workload_pod_count
        if ns.allocatable_cpu > 0:
            utilization[(node_name, pool, "cpu")] = ns.total_cpu_used / ns.allocatable_cpu
        if ns.allocatable_memory > 0:
            utilization[(node_name, pool, "memory")] = ns.total_memory_used / ns.allocatable_memory
    m.refresh_gauge(m.node_utilization_ratio, utilization)
    m.refresh_gauge(m.workload_pods, {(pool_name,): count for pool_name, count in pool_pod_counts.items()})

    # Instrumentation point 3: pending pods
    burst_untaint = check_pending_pods(cfg, node_states, pending_pods)
    m.pending_pods_compatible.set(len(pending_pods))

    desired_taint, desired_untaint, mandatory_untaint = compute_taints(node_states, cfg)

    # Merge burst untaint
    desired_untaint |= burst_untaint
    desired_taint -= burst_untaint

    # Instrumentation point 4: tainted nodes per nodepool (after compute)
    pool_taint_counts: dict[str, int] = {}
    for node_name, ns in node_states.items():
        pool = managed_names.get(node_name, "unknown")
        will_be_tainted = (ns.is_tainted or node_name in desired_taint) and node_name not in desired_untaint
        if will_be_tainted:
            pool_taint_counts[pool] = pool_taint_counts.get(pool, 0) + 1
    m.refresh_gauge(m.tainted_nodes, {(pool_name,): count for pool_name, count in pool_taint_counts.items()})

    # Apply cooldown: don't untaint nodes that were recently tainted.
    # Bypass cooldown for:
    #   - burst untaint (urgent need overrides hysteresis)
    #   - mandatory untaint (min_nodes enforcement is a safety invariant)
    cooldown_exempt = burst_untaint | mandatory_untaint
    now = time.time()
    cooldown_blocked: set[str] = set()
    for node_name in desired_untaint - cooldown_exempt:
        last_tainted = taint_times.get(node_name, 0)
        if now - last_tainted < cfg.taint_cooldown:
            log.debug(
                "Skipping untaint of %s: within cooldown (%.0fs remaining)",
                node_name,
                cfg.taint_cooldown - (now - last_tainted),
            )
            cooldown_blocked.add(node_name)
    desired_untaint -= cooldown_blocked

    # Instrumentation point 6: cooldown blocks
    if cooldown_blocked:
        m.cooldown_blocks_total.inc(len(cooldown_blocked))

    changes = 0
    for node_name in desired_untaint:
        ns = node_states.get(node_name)
        if ns and ns.is_tainted:
            # Determine untaint action type for metric labels
            if node_name in burst_untaint:
                action = "burst_untaint"
            elif node_name in mandatory_untaint:
                action = "mandatory_untaint"
            else:
                action = "untaint"
            try:
                remove_taint(client, node_name, cfg.taint_key, cfg.dry_run)
                m.taint_operations_total.labels(action=action, status="success").inc()
                changes += 1
            except ApiError as e:
                if e.status.code == 404:
                    log.info("Node %s disappeared (likely deleted by Karpenter), skipping", node_name)
                else:
                    log.exception("Failed to untaint node %s", node_name)
                    m.taint_operations_total.labels(action=action, status="error").inc()
            except Exception:
                log.exception("Failed to untaint node %s", node_name)
                m.taint_operations_total.labels(action=action, status="error").inc()

    for node_name in desired_taint:
        ns = node_states.get(node_name)
        if ns and not ns.is_tainted:
            try:
                apply_taint(client, node_name, cfg.taint_key, cfg.dry_run)
                taint_times[node_name] = time.time()
                m.taint_operations_total.labels(action="taint", status="success").inc()
                changes += 1
            except ApiError as e:
                if e.status.code == 404:
                    log.info("Node %s disappeared (likely deleted by Karpenter), skipping", node_name)
                else:
                    log.exception("Failed to taint node %s", node_name)
                    m.taint_operations_total.labels(action="taint", status="error").inc()
            except Exception:
                log.exception("Failed to taint node %s", node_name)
                m.taint_operations_total.labels(action="taint", status="error").inc()

    if changes:
        log.info("Applied %d taint change(s)", changes)
    else:
        log.debug("No taint changes needed")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    cfg = Config.from_env()
    log.info("Node Compactor starting")
    log.info(
        "Config: interval=%ds, max_uptime=%dh, min_nodes=%d, taint_cooldown=%ds, dry_run=%s",
        cfg.interval,
        cfg.max_uptime_hours,
        cfg.min_nodes,
        cfg.taint_cooldown,
        cfg.dry_run,
    )

    # Expose Prometheus metrics on :8080/metrics
    start_http_server(8080)
    log.info("Prometheus metrics server started on :8080")

    m.config_info.info(
        {
            "interval": str(cfg.interval),
            "max_uptime_hours": str(cfg.max_uptime_hours),
            "min_nodes": str(cfg.min_nodes),
            "taint_cooldown": str(cfg.taint_cooldown),
            "dry_run": str(cfg.dry_run),
            "taint_key": cfg.taint_key,
            "nodepool_label": cfg.nodepool_label,
        }
    )

    client = Client()
    shutdown = False
    taint_times: dict[str, float] = {}

    def handle_signal(signum, frame):
        nonlocal shutdown
        log.info("Received signal %d, shutting down...", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # No startup cleanup -- the first reconcile() computes the correct
    # taint state and applies it. Nodes that shouldn't be tainted get
    # untainted. This avoids the scheduling window that a blind
    # cleanup_stale_taints() would create.

    while not shutdown:
        try:
            with m.reconcile_duration_seconds.time():
                reconcile(client, cfg, taint_times)
            m.reconcile_cycles_total.labels(status="success").inc()
        except Exception:
            m.reconcile_cycles_total.labels(status="error").inc()
            log.exception("Reconciliation failed (will retry next cycle)")

        for _ in range(cfg.interval * 10):
            if shutdown:
                break
            time.sleep(0.1)

    # Clean up all compactor taints on graceful shutdown
    log.info("Cleaning up taints before shutdown...")
    try:
        cleanup_stale_taints(client, cfg)
    except Exception:
        log.exception("Failed to clean up taints during shutdown")

    log.info("Node Compactor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
