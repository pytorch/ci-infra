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
import math
import pathlib
import signal
import sys
import time

import metrics as m
from discovery import build_node_states, discover_managed_nodes
from lightkube import ApiError, Client
from models import LABEL_NODE_FLEET, Config, NodeState
from packing import _count_spare_nodes, compute_taints, select_reserved_nodes
from phantom import apply_pending_phantom_load
from prometheus_client import start_http_server
from reservations import cleanup_reservations, reconcile_reservations
from taints import (
    apply_taint,
    check_pending_pods,
    cleanup_stale_taints,
    remove_taint,
)

log = logging.getLogger("compactor")


def _fleet_group_key(ns: NodeState) -> str:
    """Group nodes by fleet (node-fleet label), falling back to nodepool."""
    return ns.labels.get(LABEL_NODE_FLEET) or ns.nodepool


# ============================================================================
# Reconciliation loop
# ============================================================================


def reconcile(
    client: Client,
    cfg: Config,
    taint_times: dict[str, float],
    fleet_cooldown_times: dict[str, float] | None = None,
    peak_history: dict[str, list[tuple[float, int]]] | None = None,
) -> None:
    """Single reconciliation cycle.

    Args:
        client: Kubernetes API client.
        cfg: Compactor configuration.
        taint_times: Mutable dict tracking when each node was last tainted.
            Updated in-place when nodes are tainted.
        fleet_cooldown_times: Mutable dict tracking when each nodepool last
            had a burst untaint. Updated in-place. If None, fleet cooldown
            is effectively disabled.
        peak_history: Mutable dict tracking per-fleet sliding window of
            (timestamp, bin_pack_min_needed) samples. Threaded into
            compute_taints so the peak-window floor survives across cycles.
            If None, the peak-window floor is effectively disabled.
    """
    # Touch healthcheck file so liveness probe passes even with no managed nodes.
    pathlib.Path("/tmp/healthy").touch()

    managed_names = discover_managed_nodes(client, cfg)
    if not managed_names:
        log.debug("No managed nodes found (no NodePools with label %s)", cfg.nodepool_label)
        m.refresh_gauge(m.managed_nodes, {})
        m.refresh_gauge(m.node_utilization_ratio, {})
        m.refresh_gauge(m.workload_pods, {})
        m.refresh_gauge(m.tainted_nodes, {})
        return

    # Instrumentation point 1: managed nodes per fleet
    # (deferred until fleet_groups is built, after node_states)

    node_states, pending_pods = build_node_states(client, cfg, managed_names)
    if not node_states:
        log.debug("No node states built")
        m.refresh_gauge(m.node_utilization_ratio, {})
        m.refresh_gauge(m.workload_pods, {})
        m.refresh_gauge(m.tainted_nodes, {})
        return

    # Apply phantom load from pending pods before any utilization-based decisions.
    # This makes the compactor "see" pods that are about to land, preventing
    # premature tainting of nodes that will soon be needed.
    apply_pending_phantom_load(node_states, pending_pods, cfg)

    total_pods = sum(ns.workload_pod_count for ns in node_states.values())
    total_nodes = len(node_states)
    tainted = sum(1 for ns in node_states.values() if ns.is_tainted)
    log.info(
        "Reconciling: %d nodes (%d tainted), %d workload pods",
        total_nodes,
        tainted,
        total_pods,
    )

    # Instrumentation point 2: workload pods per fleet, utilization per node
    fleet_pod_counts: dict[str, int] = {}
    utilization: dict[tuple[str, ...], float] = {}
    for node_name, ns in node_states.items():
        pool = managed_names.get(node_name, "unknown")
        fk = _fleet_group_key(ns)
        fleet_pod_counts[fk] = fleet_pod_counts.get(fk, 0) + ns.workload_pod_count
        if ns.allocatable_cpu > 0:
            utilization[(node_name, pool, "cpu")] = ns.total_cpu_used / ns.allocatable_cpu
        if ns.allocatable_memory > 0:
            utilization[(node_name, pool, "memory")] = ns.total_memory_used / ns.allocatable_memory
        if ns.allocatable_gpu > 0:
            utilization[(node_name, pool, "gpu")] = ns.total_gpu_used / ns.allocatable_gpu
    m.refresh_gauge(m.node_utilization_ratio, utilization)
    m.refresh_gauge(m.workload_pods, {(fk,): count for fk, count in fleet_pod_counts.items()})

    # Instrumentation point 3: pending pods
    burst_untaint, compatible_count = check_pending_pods(cfg, node_states, pending_pods)
    m.pending_pods_compatible.set(compatible_count)

    # Record fleet cooldown for fleets that had burst untaints
    if fleet_cooldown_times is not None:
        for node_name in burst_untaint:
            ns = node_states.get(node_name)
            if ns:
                fleet_cooldown_times[_fleet_group_key(ns)] = time.time()

    # Build fleet_groups (for fleet-level metrics/decisions) and pool_groups
    # (for per-nodepool metrics like reservations).
    fleet_groups: dict[str, list] = {}
    pool_groups: dict[str, list] = {}
    for _node_name, ns in node_states.items():
        fleet_key = _fleet_group_key(ns)
        fleet_groups.setdefault(fleet_key, []).append(ns)
        pool_groups.setdefault(ns.nodepool, []).append(ns)

    # Instrumentation point 1: managed nodes per fleet (deferred from above)
    m.refresh_gauge(m.managed_nodes, {(fk,): float(len(nodes)) for fk, nodes in fleet_groups.items()})

    # Capacity reservation: protect nodes from Karpenter deletion (before compute_taints).
    all_reserved: set[str] = set()
    if cfg.capacity_reservation_nodes > 0:
        pool_reserved = select_reserved_nodes(pool_groups, cfg)
        for names in pool_reserved.values():
            all_reserved |= names

    # Instrumentation: reserved nodes per pool
    reserved_counts: dict[tuple[str, ...], float] = {}
    if cfg.capacity_reservation_nodes > 0:
        for pool, nodes_list in pool_groups.items():
            reserved_counts[(pool,)] = float(sum(1 for n in nodes_list if n.name in all_reserved))
    m.refresh_gauge(m.reserved_nodes, reserved_counts)

    desired_taint, desired_untaint, mandatory_untaint, rate_limited = compute_taints(
        node_states,
        cfg,
        reserved_nodes=all_reserved,
        group_key=_fleet_group_key,
        peak_history=peak_history,
        pending_pods=pending_pods,
    )

    # Log and emit metrics for rate-limited nodes
    if rate_limited:
        log.info("Rate-limited %d node(s) from tainting: %s", len(rate_limited), sorted(rate_limited))
        # Emit per-pool rate limit metrics
        pool_rate_limited: dict[str, int] = {}
        for node_name in rate_limited:
            ns = node_states.get(node_name)
            pool = managed_names.get(node_name, "unknown") if ns is None else ns.nodepool
            pool_rate_limited[pool] = pool_rate_limited.get(pool, 0) + 1
        for pool, count in pool_rate_limited.items():
            m.rate_limit_blocks.labels(nodepool=pool).inc(count)

    # Instrumentation: spare capacity per fleet
    spare_actual: dict[tuple[str, ...], float] = {}
    spare_req: dict[tuple[str, ...], float] = {}
    for fleet_key, nodes_in_fleet in fleet_groups.items():
        required = max(
            cfg.spare_capacity_nodes,
            math.ceil(len(nodes_in_fleet) * cfg.spare_capacity_ratio),
        )
        spare_req[(fleet_key,)] = float(required)
        spare_actual[(fleet_key,)] = float(
            _count_spare_nodes(nodes_in_fleet, None, desired_taint, cfg.spare_capacity_threshold)
        )
    m.refresh_gauge(m.spare_capacity_gauge, spare_actual)
    m.refresh_gauge(m.spare_capacity_required, spare_req)

    # Merge burst untaint
    desired_untaint |= burst_untaint
    desired_taint -= burst_untaint

    # Fleet cooldown: block new taints in fleets that recently had a burst untaint.
    now_fleet = time.time()
    fleet_blocked: set[str] = set()
    if fleet_cooldown_times is not None and cfg.fleet_cooldown > 0:
        for node_name in list(desired_taint):
            ns = node_states.get(node_name)
            if ns and _fleet_group_key(ns) in fleet_cooldown_times:
                fleet_nodes = [n for n in node_states.values() if _fleet_group_key(n) == _fleet_group_key(ns)]
                surplus_count = sum(1 for n in fleet_nodes if n.name in desired_taint or n.is_tainted)
                # Override: halve cooldown if >50% of fleet is surplus
                effective_cooldown = cfg.fleet_cooldown
                if surplus_count > len(fleet_nodes) * 0.5:
                    effective_cooldown = cfg.fleet_cooldown // 2

                elapsed = now_fleet - fleet_cooldown_times[_fleet_group_key(ns)]
                if elapsed < effective_cooldown:
                    desired_taint.discard(node_name)
                    fleet_blocked.add(node_name)

        # Log and emit metrics for fleet-blocked nodes
        if fleet_blocked:
            log.info(
                "Fleet cooldown blocked %d taint(s): %s",
                len(fleet_blocked),
                ", ".join(sorted(fleet_blocked)),
            )
            # Count blocks per fleet
            fleet_block_counts: dict[str, int] = {}
            for node_name in fleet_blocked:
                ns = node_states.get(node_name)
                if ns:
                    fk = _fleet_group_key(ns)
                    fleet_block_counts[fk] = fleet_block_counts.get(fk, 0) + 1
            for fleet_key, count in fleet_block_counts.items():
                m.fleet_cooldown_blocks.labels(fleet=fleet_key).inc(count)

        # Emit fleet_cooldown_remaining gauge per fleet
        cooldown_remaining: dict[tuple[str, ...], float] = {}
        for fleet_key, last_burst_time in fleet_cooldown_times.items():
            remaining = max(0.0, cfg.fleet_cooldown - (now_fleet - last_burst_time))
            cooldown_remaining[(fleet_key,)] = remaining
        m.refresh_gauge(m.fleet_cooldown_remaining, cooldown_remaining)

    # Instrumentation point 4: tainted nodes per fleet (after compute)
    fleet_taint_counts: dict[str, int] = {}
    for node_name, ns in node_states.items():
        will_be_tainted = (ns.is_tainted or node_name in desired_taint) and node_name not in desired_untaint
        if will_be_tainted:
            fk = _fleet_group_key(ns)
            fleet_taint_counts[fk] = fleet_taint_counts.get(fk, 0) + 1
    m.refresh_gauge(m.tainted_nodes, {(fk,): count for fk, count in fleet_taint_counts.items()})

    # Apply cooldown: skip untaint for recently tainted nodes.
    # Exempt: burst untaint (urgent) and mandatory untaint (min_nodes safety).
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

    # Reconcile capacity-reservation annotations after taints are applied.
    if cfg.capacity_reservation_nodes > 0:
        reconcile_reservations(
            client,
            list(node_states.values()),
            all_reserved,
            cfg.dry_run,
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    cfg = Config.from_env()
    log.info("Node Compactor starting")
    log.info(
        "Config: interval=%ds, max_uptime=%dh, min_nodes=%d, dry_run=%s, "
        "min_node_age=%ds, taint_cooldown=%ds, taint_rate=%.2f, fleet_cooldown=%ds, "
        "spare_capacity_nodes=%d, spare_capacity_ratio=%.2f, "
        "spare_capacity_threshold=%.2f, capacity_reservation_nodes=%d, "
        "nodepool_label=%s, taint_key=%s",
        cfg.interval,
        cfg.max_uptime_hours,
        cfg.min_nodes,
        cfg.dry_run,
        cfg.min_node_age,
        cfg.taint_cooldown,
        cfg.taint_rate,
        cfg.fleet_cooldown,
        cfg.spare_capacity_nodes,
        cfg.spare_capacity_ratio,
        cfg.spare_capacity_threshold,
        cfg.capacity_reservation_nodes,
        cfg.nodepool_label,
        cfg.taint_key,
    )
    log.info(
        "Peak-window tracking: window=%ds, pending pod max age for bin-pack=%ds, pending pod min age=%ds",
        cfg.peak_window_seconds,
        cfg.pending_pod_max_age_seconds,
        cfg.pending_pod_min_age_seconds,
    )

    # Expose Prometheus metrics on :8080/metrics. Bind ::0 so the socket
    # accepts both IPv6 and IPv4-mapped IPv6 connections — the pod has
    # only an IPv6 address on IPv6-only EKS but ServiceMonitor scrapes
    # may originate from either family.
    start_http_server(8080, addr="::")
    log.info("Prometheus metrics server started on :8080")

    m.config_info.info(
        {
            "interval": str(cfg.interval),
            "max_uptime_hours": str(cfg.max_uptime_hours),
            "min_nodes": str(cfg.min_nodes),
            "taint_cooldown": str(cfg.taint_cooldown),
            "fleet_cooldown": str(cfg.fleet_cooldown),
            "dry_run": str(cfg.dry_run),
            "taint_key": cfg.taint_key,
            "nodepool_label": cfg.nodepool_label,
        }
    )

    client = Client()
    shutdown = False
    taint_times: dict[str, float] = {}
    fleet_cooldown_times: dict[str, float] = {}
    # Sliding-window samples of per-fleet bin-pack peak demand. Lives only in
    # process memory — restart clears the floor, which is fine because the
    # next reconcile rebuilds it from observed load.
    peak_history: dict[str, list[tuple[float, int]]] = {}

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
                reconcile(client, cfg, taint_times, fleet_cooldown_times, peak_history)
            m.reconcile_cycles_total.labels(status="success").inc()
        except ApiError as e:
            m.reconcile_cycles_total.labels(status="error").inc()
            if e.status.code == 401:
                log.warning("Got 401 Unauthorized — recreating client (SA token likely rotated by kubelet)")
                client = Client()
            else:
                log.exception("Reconciliation failed (will retry next cycle)")
        except Exception:
            m.reconcile_cycles_total.labels(status="error").inc()
            log.exception("Reconciliation failed (will retry next cycle)")

        for _ in range(cfg.interval * 10):
            if shutdown:
                break
            time.sleep(0.1)

    # Clean up all compactor taints and reservations on graceful shutdown
    log.info("Cleaning up taints and reservations before shutdown...")
    try:
        cleanup_stale_taints(client, cfg)
    except Exception:
        log.exception("Failed to clean up taints during shutdown")
    try:
        cleanup_reservations(client)
    except Exception:
        log.exception("Failed to clean up reservations during shutdown")

    log.info("Node Compactor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
