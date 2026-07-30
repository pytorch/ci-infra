"""GitHub Actions runner busy-state detection and protection classifier.

Runner pods are owned by an EphemeralRunner custom resource and stay eligible for
age-based cleanup, unlike controller-managed pods. This module distinguishes an
idle runner (reapable) from one actively executing a job (protected): a runner is
busy when its EphemeralRunner carries a status.jobId, or when a live workflow/step
job pod pins it via the runner-pod label.

classify_pod is the single decision function shared by the find pass and the
pre-delete recheck, so the two paths cannot drift. The find pass feeds it liveness
resolved from one batch listing; the recheck feeds it liveness resolved from a
targeted per-pod GET.
"""

import logging
from dataclasses import dataclass
from enum import Enum

from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Pod

log = logging.getLogger("zombie-cleanup")

# GitHub Actions runner pods carry this ownerReference kind. Deliberately not a
# controller-managed kind: idle runners stay reapable, busy ones are gated instead.
EPHEMERAL_RUNNER_KIND = "EphemeralRunner"

# Bare workflow/step job pods carry this label; its value is the owning runner
# pod's name and is the join key back to the runner. Kubernetes label VALUES are
# capped at 63 chars (pod names may reach 253), so this join holds only because
# OSDC runner names stay well under 63 (the ~42-char runner-naming convention);
# a longer runner name would be truncated in the label value and break the match.
RUNNER_POD_LABEL = "runner-pod"

# Terminal pod phases never count as busy and never pin a runner.
TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})

EphemeralRunner = create_namespaced_resource("actions.github.com", "v1alpha1", "EphemeralRunner", "ephemeralrunners")


class Decision(Enum):
    """Outcome of the shared protection classifier for one pod.

    The value doubles as a human-readable reason for log lines. is_delete marks
    the two outcomes that lead to a deletion; every other outcome keeps the pod.
    """

    FAIL_SAFE_PROTECT = "fail-safe-protect"
    BUSY_PROTECT = "busy-protect"
    JOBPOD_PROTECT = "jobpod-protect"
    HARD_CAP_DELETE = "hard-cap-delete"
    NORMAL_AGE_DELETE = "normal-age-delete"
    NOT_OVER_AGE_KEEP = "not-over-age-keep"

    @property
    def is_delete(self) -> bool:
        return self in (Decision.HARD_CAP_DELETE, Decision.NORMAL_AGE_DELETE)


@dataclass
class RunnerLiveness:
    """Per-pod inputs for classify_pod.

    Resolved either from a batch snapshot (BusyState.liveness_for) or from a
    targeted GET (recheck_liveness), so the classifier itself stays path-agnostic.
    """

    read_failed: bool = False
    runner_busy: bool = False
    anchor_present: bool = False


@dataclass
class BusyState:
    """Runner busy-ness derived from one pod listing plus one EphemeralRunner listing."""

    er_jobid: dict[str, str]
    live_runner_names: set[str]
    busy_by_workflow: set[str]
    er_read_failed: bool

    def is_runner_busy(self, pod_name: str) -> bool:
        """True if the runner has an assigned jobId or a live job pod points at it."""
        return self.er_jobid.get(pod_name, "") != "" or pod_name in self.busy_by_workflow

    def liveness_for(self, pod: Pod) -> RunnerLiveness:
        """Resolve classify_pod inputs for a pod from this batch snapshot."""
        if is_ephemeralrunner_pod(pod):
            return RunnerLiveness(read_failed=self.er_read_failed, runner_busy=self.is_runner_busy(pod.metadata.name))
        label = get_runner_pod_label(pod)
        if label is not None:
            return RunnerLiveness(anchor_present=label in self.live_runner_names)
        return RunnerLiveness()


def is_ephemeralrunner_pod(pod: Pod) -> bool:
    """Check if pod is a GitHub Actions runner pod (owned by an EphemeralRunner)."""
    refs = pod.metadata.ownerReferences
    if not refs:
        return False
    return any(ref.kind == EPHEMERAL_RUNNER_KIND for ref in refs)


def get_runner_pod_label(pod: Pod) -> str | None:
    """Return the runner-pod label value (owning runner name), or None if absent."""
    labels = pod.metadata.labels
    if not labels:
        return None
    return labels.get(RUNNER_POD_LABEL)


def pod_phase(pod: Pod) -> str | None:
    """Phase string from a pod's status, or None if unset."""
    return pod.status.phase if pod.status else None


def age_over_threshold(phase: str | None, age_hours: float, pending_max: int, running_max: int) -> int | None:
    """Return the exceeded age threshold (hours) for a pod, or None if within limits."""
    if phase == "Pending" and age_hours > pending_max:
        return pending_max
    if phase in ("Running", "Unknown") and age_hours > running_max:
        return running_max
    return None


def classify_pod(
    pod: Pod,
    phase: str | None,
    age_hours: float,
    pending_max: int,
    running_max: int,
    busy_max: int,
    liveness: RunnerLiveness,
) -> Decision:
    """Single protection classifier shared by the find and pre-delete-recheck paths.

    Runner pods (EphemeralRunner-owned) and bare workflow/step pods (runner-pod
    label) are protected while live, but both face an absolute busy hard cap: past
    busy_max they are presumed hung and deleted even when still live. A failed
    liveness read protects the pod (fail-safe). Everything else falls through to
    the plain age thresholds.
    """
    if is_ephemeralrunner_pod(pod):
        if liveness.read_failed:
            return Decision.FAIL_SAFE_PROTECT
        # A terminal (Succeeded/Failed) runner is finished and never busy; a stale
        # workflow-pod pin must not resurrect it into the hard-cap path (ARC GC owns it).
        if liveness.runner_busy and phase not in TERMINAL_PHASES:
            return Decision.HARD_CAP_DELETE if age_hours > busy_max else Decision.BUSY_PROTECT
        return _age_decision(phase, age_hours, pending_max, running_max)
    if get_runner_pod_label(pod) is not None:
        if liveness.read_failed:
            return Decision.FAIL_SAFE_PROTECT
        if liveness.anchor_present:
            return Decision.HARD_CAP_DELETE if age_hours > busy_max else Decision.JOBPOD_PROTECT
        return _age_decision(phase, age_hours, pending_max, running_max)
    return _age_decision(phase, age_hours, pending_max, running_max)


def _age_decision(phase: str | None, age_hours: float, pending_max: int, running_max: int) -> Decision:
    if age_over_threshold(phase, age_hours, pending_max, running_max) is not None:
        return Decision.NORMAL_AGE_DELETE
    return Decision.NOT_OVER_AGE_KEEP


def read_ephemeralrunner_jobids(client: Client, namespace: str) -> tuple[dict[str, str], bool]:
    """List EphemeralRunners once and map runner name -> status.jobId ("" if none).

    On ANY error returns ({}, True) so callers can treat every runner pod as
    protected. status may be None on a freshly created EphemeralRunner.
    """
    er_jobid: dict[str, str] = {}
    try:
        for er in client.list(EphemeralRunner, namespace=namespace):
            status = er.status or {}
            er_jobid[er.metadata.name] = status.get("jobId") or ""
    except Exception:
        log.exception("Failed to list EphemeralRunners in %s; treating runner pods as protected", namespace)
        return {}, True
    return er_jobid, False


def build_busy_sets(pods: list[Pod]) -> tuple[set[str], set[str]]:
    """From an already-fetched pod list return (live_runner_names, busy_by_workflow).

    Terminal (Succeeded/Failed) pods are ignored entirely: they neither pin a
    runner nor count as a live runner. live_runner_names therefore holds ONLY
    NON-TERMINAL EphemeralRunner-owned runner pod names, so a bare pod that
    self-applies the runner-pod label cannot anchor its own protection, and a
    lingering terminal runner pod cannot pin its orphaned workflow/step pods.
    """
    live_runner_names: set[str] = set()
    busy_by_workflow: set[str] = set()
    for pod in pods:
        if pod_phase(pod) in TERMINAL_PHASES:
            continue
        if is_ephemeralrunner_pod(pod):
            live_runner_names.add(pod.metadata.name)
        label = get_runner_pod_label(pod)
        if label:
            busy_by_workflow.add(label)
    return live_runner_names, busy_by_workflow


def gather_busy_state(client: Client, namespace: str, pods: list[Pod]) -> BusyState:
    """Build a BusyState from one pod listing plus a fresh EphemeralRunner listing."""
    er_jobid, er_read_failed = read_ephemeralrunner_jobids(client, namespace)
    live_runner_names, busy_by_workflow = build_busy_sets(pods)
    return BusyState(er_jobid, live_runner_names, busy_by_workflow, er_read_failed)


def get_ephemeralrunner(client: Client, name: str, namespace: str) -> tuple[bool, str, bool]:
    """Targeted EphemeralRunner GET for the pre-delete recheck.

    Returns (found, job_id, read_failed):
      - (True, job_id, False)  -> ER exists (job_id "" when idle)
      - (False, "", False)     -> 404: ER gone, i.e. the runner finished
      - (False, "", True)      -> any other error; caller must fail safe
    """
    try:
        er = client.get(EphemeralRunner, name=name, namespace=namespace)
    except ApiError as e:
        if e.status.code == 404:
            return False, "", False
        log.exception("Recheck: EphemeralRunner GET %s failed (HTTP %s)", name, e.status.code)
        return False, "", True
    except Exception:
        log.exception("Recheck: EphemeralRunner GET %s failed", name)
        return False, "", True
    status = er.status or {}
    return True, status.get("jobId") or "", False


def recheck_liveness(client: Client, pod: Pod, namespace: str) -> RunnerLiveness:
    """Fetch fresh per-pod liveness for one delete candidate via a targeted GET.

    A runner pod GETs its own EphemeralRunner (busy = status.jobId set); a job pod
    GETs its anchor runner (present = GET succeeds). Any non-404 error yields
    read_failed so classify_pod protects the pod. Bare pods need no GET.
    """
    if is_ephemeralrunner_pod(pod):
        found, job_id, read_failed = get_ephemeralrunner(client, pod.metadata.name, namespace)
        if read_failed:
            return RunnerLiveness(read_failed=True)
        return RunnerLiveness(runner_busy=found and job_id != "")
    label = get_runner_pod_label(pod)
    if label is not None:
        found, _job_id, read_failed = get_ephemeralrunner(client, label, namespace)
        if read_failed:
            return RunnerLiveness(read_failed=True)
        return RunnerLiveness(anchor_present=found)
    return RunnerLiveness()
