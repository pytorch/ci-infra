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

from discovery import build_node_states, discover_managed_nodes
from lightkube import ApiError, Client
from models import Config
from packing import compute_taints
from taints import apply_taint, check_pending_pods, cleanup_stale_taints, remove_taint


log = logging.getLogger("compactor")


# ============================================================================
# Reconciliation loop
# ============================================================================


def reconcile(
    client: Client, cfg: Config, taint_times: dict[str, float]
) -> None:
    """Single reconciliation cycle.

    Args:
        client: Kubernetes API client.
        cfg: Compactor configuration.
        taint_times: Mutable dict tracking when each node was last tainted.
            Updated in-place when nodes are tainted.
    """
    managed_names = discover_managed_nodes(client, cfg)
    if not managed_names:
        log.debug("No managed nodes found (no NodePools with label %s)", cfg.nodepool_label)
        return

    node_states, pending_pods = build_node_states(client, cfg, managed_names)
    if not node_states:
        log.debug("No node states built")
        return

    total_pods = sum(ns.workload_pod_count for ns in node_states.values())
    total_nodes = len(node_states)
    tainted = sum(1 for ns in node_states.values() if ns.is_tainted)
    log.info(
        "Reconciling: %d nodes (%d tainted), %d workload pods",
        total_nodes, tainted, total_pods,
    )

    # Check for pending pods first -- untaint before tainting
    burst_untaint = check_pending_pods(cfg, node_states, pending_pods)

    desired_taint, desired_untaint = compute_taints(node_states, cfg)

    # Merge burst untaint
    desired_untaint |= burst_untaint
    desired_taint -= burst_untaint

    # Apply cooldown: don't untaint nodes that were recently tainted
    # (burst untaint bypasses cooldown -- urgent need overrides hysteresis)
    now = time.time()
    cooldown_blocked: set[str] = set()
    for node_name in desired_untaint - burst_untaint:
        last_tainted = taint_times.get(node_name, 0)
        if now - last_tainted < cfg.taint_cooldown:
            log.debug(
                "Skipping untaint of %s: within cooldown (%.0fs remaining)",
                node_name,
                cfg.taint_cooldown - (now - last_tainted),
            )
            cooldown_blocked.add(node_name)
    desired_untaint -= cooldown_blocked

    changes = 0
    for node_name in desired_untaint:
        ns = node_states.get(node_name)
        if ns and ns.is_tainted:
            try:
                remove_taint(client, node_name, cfg.taint_key, cfg.dry_run)
                changes += 1
            except ApiError as e:
                if e.status.code == 404:
                    log.info("Node %s disappeared (likely deleted by Karpenter), skipping", node_name)
                else:
                    log.exception("Failed to untaint node %s", node_name)
            except Exception:
                log.exception("Failed to untaint node %s", node_name)

    for node_name in desired_taint:
        ns = node_states.get(node_name)
        if ns and not ns.is_tainted:
            try:
                apply_taint(client, node_name, cfg.taint_key, cfg.dry_run)
                taint_times[node_name] = time.time()
                changes += 1
            except ApiError as e:
                if e.status.code == 404:
                    log.info("Node %s disappeared (likely deleted by Karpenter), skipping", node_name)
                else:
                    log.exception("Failed to taint node %s", node_name)
            except Exception:
                log.exception("Failed to taint node %s", node_name)

    if changes:
        log.info("Applied %d taint change(s)", changes)
    else:
        log.debug("No taint changes needed")

    # Touch healthcheck file for liveness probe
    pathlib.Path("/tmp/healthy").touch()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    cfg = Config.from_env()
    log.info("Node Compactor starting")
    log.info(
        "Config: interval=%ds, max_uptime=%dh, min_nodes=%d, "
        "taint_cooldown=%ds, dry_run=%s",
        cfg.interval, cfg.max_uptime_hours, cfg.min_nodes,
        cfg.taint_cooldown, cfg.dry_run,
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
            reconcile(client, cfg, taint_times)
        except Exception:
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
