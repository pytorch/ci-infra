"""GPU black-hole detection for the Node Compactor.

A single failed GPU can silently take an entire GPU node out of service while
the node keeps reporting ``Ready`` and keeps advertising every GPU as
allocatable. ``nvidia-device-plugin``'s ``alignedAlloc`` builds the NVLink
topology across ALL GPUs on the node before filtering down to the ones the pod
actually asked for, so one lost GPU fails ``GetPreferredAllocation`` for every
GPU pod — including pods that would have landed on a healthy GPU. The kubelet
rejects each of those pods with ``UnexpectedAdmissionError`` and the scheduler
immediately sends another one, so the node behaves as a black hole: it accepts
work at full rate and destroys all of it.

Nothing in the node's own status reflects this. The only place the fault is
visible is on the pods it already killed, which is what this module reads.

Detection is deliberately a symptom check rather than a hardware probe: it
needs no NVML access, no privileged DaemonSet and no per-node agent, and it
fires on any device-plugin allocation fault rather than only the ones a
hardware probe was written to anticipate. The cost is that it is inherently
post-hoc — it cannot fire until ``threshold`` pods have already died.
"""

import logging
from datetime import UTC, datetime

from models import Config, NodeState

log = logging.getLogger("compactor")

# Kubelet's catch-all admission rejection reason. Not GPU-specific on its own:
# pkg/kubelet/lifecycle/predicate.go documents it as "an error during admission
# that could not be categorized", so the message signature below is what makes
# the match specific.
ADMISSION_FAILURE_REASON = "UnexpectedAdmissionError"

# Substring of the kubelet's rejection message identifying a device-plugin
# preferred-allocation failure. The full message looks like:
#   Pod was rejected: Allocate failed due to device plugin GetPreferredAllocation
#   rpc failed with err: rpc error: code = Unknown desc = error getting list of
#   preferred allocation devices: unable to get device link information: error
#   getting NVLink for devices (0, 1): failed to get nvlink remote pci info:
#   failed to get nvlink state: GPU is lost, which is unexpected
# NOTE: TopologyAffinityError is a *different* typed admission reason and is
# intentionally not matched here.
ADMISSION_FAILURE_SIGNATURE = "GetPreferredAllocation"


def admission_failure_time(pod) -> datetime | None:
    """Return the pod's creation time if it died of a device-plugin allocation failure.

    Returns None for any pod that failed for another reason, so the caller can
    feed every Failed pod through this without pre-filtering.

    Creation time is used rather than ``status.startTime`` because a pod
    rejected at admission never starts, so ``startTime`` is unset.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return None
    if getattr(status, "reason", None) != ADMISSION_FAILURE_REASON:
        return None
    message = getattr(status, "message", None) or ""
    if ADMISSION_FAILURE_SIGNATURE not in message:
        return None

    meta = getattr(pod, "metadata", None)
    return getattr(meta, "creationTimestamp", None) if meta else None


def count_recent_failures(ns: NodeState, cfg: Config, now: datetime) -> int:
    """Count this node's device-plugin admission failures inside the detection window.

    Windowing matters for more than promptness: rejected pod objects are left
    behind in Failed phase and are not always reaped, so an unbounded count
    would keep growing off stale corpses long after the fault.
    """
    cutoff = cfg.gpu_quarantine_window_seconds
    return sum(1 for ts in ns.admission_failures if ts and (now - ts).total_seconds() <= cutoff)


def select_quarantine_nodes(
    node_states: dict[str, NodeState],
    cfg: Config,
    group_key,
    now: datetime | None = None,
) -> tuple[set[str], dict[str, int]]:
    """Pick GPU nodes to quarantine with a NoSchedule taint.

    A node qualifies when it advertises GPUs and its kubelet has rejected at
    least ``gpu_quarantine_threshold`` pods with a device-plugin allocation
    failure inside the detection window.

    Selection is capped per fleet at ``gpu_quarantine_max_fleet_ratio`` of the
    fleet's nodes (already-quarantined nodes count against the cap). Without
    this a cluster-wide fault — a bad AMI, a bad driver rollout — would cordon
    every GPU node at once and turn a degraded fleet into no fleet. Karpenter's
    own node-repair path applies the same 20% guard for the same reason.

    Returns (nodes_to_quarantine, per-node window failure counts). The counts
    cover every GPU node with at least one failure, not just the selected ones,
    so the metric shows pressure building before the threshold trips.
    """
    if not cfg.gpu_quarantine_enabled:
        return set(), {}

    now = now or datetime.now(UTC)

    failure_counts: dict[str, int] = {}
    candidates: list[NodeState] = []
    for ns in node_states.values():
        if ns.allocatable_gpu <= 0:
            continue
        count = count_recent_failures(ns, cfg, now)
        if count:
            failure_counts[ns.name] = count
        if ns.is_gpu_quarantined:
            continue
        if count >= cfg.gpu_quarantine_threshold:
            candidates.append(ns)

    if not candidates:
        return set(), failure_counts

    # Group every GPU node by fleet so the cap is computed against fleet size,
    # not against the candidate list.
    fleet_sizes: dict[str, int] = {}
    fleet_already_quarantined: dict[str, int] = {}
    for ns in node_states.values():
        if ns.allocatable_gpu <= 0:
            continue
        fleet = group_key(ns)
        fleet_sizes[fleet] = fleet_sizes.get(fleet, 0) + 1
        if ns.is_gpu_quarantined:
            fleet_already_quarantined[fleet] = fleet_already_quarantined.get(fleet, 0) + 1

    # Worst offenders first, name as tie-break so the choice is deterministic
    # when a whole fleet is failing and the cap has to drop some.
    candidates.sort(key=lambda n: (-failure_counts[n.name], n.name))

    selected: set[str] = set()
    per_fleet_new: dict[str, int] = {}
    for ns in candidates:
        fleet = group_key(ns)
        cap = max(1, int(fleet_sizes[fleet] * cfg.gpu_quarantine_max_fleet_ratio))
        used = fleet_already_quarantined.get(fleet, 0) + per_fleet_new.get(fleet, 0)
        if used >= cap:
            log.warning(
                "GPU quarantine cap reached for fleet %s (%d/%d nodes): "
                "NOT quarantining %s despite %d admission failure(s) in %ds. "
                "A fleet-wide fault needs an operator, not more cordons.",
                fleet,
                used,
                cap,
                ns.name,
                failure_counts[ns.name],
                cfg.gpu_quarantine_window_seconds,
            )
            continue
        selected.add(ns.name)
        per_fleet_new[fleet] = per_fleet_new.get(fleet, 0) + 1
        log.error(
            "GPU black hole detected on %s (fleet %s): %d pod(s) rejected with "
            "a device plugin allocation failure in the last %ds while the node "
            "still advertises %d allocatable GPU(s). Quarantining.",
            ns.name,
            fleet,
            failure_counts[ns.name],
            cfg.gpu_quarantine_window_seconds,
            ns.allocatable_gpu,
        )

    return selected, failure_counts
