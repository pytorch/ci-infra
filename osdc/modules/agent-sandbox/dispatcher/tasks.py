"""The task lifecycle: reserve a slot, run one Job to completion, keep the result.

Sits between the HTTP surface and kube.py. It owns the only mutable state in the
process — the in-memory task table — and it is the one place that knows a task is
"a Job you poll until it stops".

It calls kube through the module (`kube.job_state(...)`, not `from kube import
job_state`) on purpose: a name imported at module load is a second binding that a test
patching `kube.job_state` would not reach, and this file's tests exist to substitute
those calls.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import kube

# Per replica. The namespace ResourceQuota is the cluster-wide bound — this exists so a
# caller gets a clean 429 instead of a wall of Jobs the quota then rejects one by one.
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "6"))
# How long /status can still answer for a finished task before its result is dropped.
RESULT_RETENTION_S = int(os.environ.get("RESULT_RETENTION_S", "3600"))
POLL_INTERVAL_S = 2

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


def run_to_completion(task_id: str, spec: dict) -> dict:
    """Create the Job, wait for it, return the result. Never raises."""
    # Grace beyond the Job's own activeDeadlineSeconds, which is the same number: without
    # it both fire at once and this loop reports a flat "did not finish" instead of the
    # Job's DeadlineExceeded, which at least names what was still running.
    deadline = time.monotonic() + kube.TASK_DEADLINE_S + POLL_INTERVAL_S * 3
    try:
        kube.create_job(task_id, spec)
    except (kube.ApiError, OSError) as exc:
        return {"errors": {"dispatch": str(exc)}}

    try:
        while True:
            if time.monotonic() > deadline:
                return {"errors": {"dispatch": f"task did not finish within {kube.TASK_DEADLINE_S}s"}}
            state, detail = kube.job_state(task_id)
            if state == "succeeded":
                return kube.task_result(task_id)
            if state == "failed":
                # A pod that died before printing (OOM, eviction, image pull) has no
                # result to parse; say so rather than reporting an empty one.
                try:
                    return kube.task_result(task_id)
                except kube.ApiError:
                    return {"errors": {"task": f"pod failed: {detail}"}}
            time.sleep(POLL_INTERVAL_S)
    except (kube.ApiError, OSError) as exc:
        return {"errors": {"dispatch": str(exc)}}
    finally:
        kube.delete_job(task_id)


def _running_locked() -> int:
    """Tasks in flight. Callers hold _TASKS_LOCK, which is not reentrant, so this cannot
    go through slots_in_use()."""
    return sum(1 for t in _TASKS.values() if t["state"] == "running")


def _prune_locked(now: float) -> None:
    """Drop finished tasks past their retention. Callers hold _TASKS_LOCK.

    Results live in memory so /status can answer after the Job is gone, which means
    this dict is the one thing in a long-running dispatcher that grows without a bound
    of its own.
    """
    stale = [tid for tid, t in _TASKS.items() if t["state"] == "done" and now - t["finished_at"] > RESULT_RETENTION_S]
    for tid in stale:
        del _TASKS[tid]


def _finish(task_id: str, result: dict) -> None:
    with _TASKS_LOCK:
        _TASKS[task_id] = {"state": "done", "result": result, "finished_at": time.monotonic()}


def slots_in_use() -> int:
    with _TASKS_LOCK:
        return _running_locked()


def start_task() -> str | None:
    """Reserve a slot and return its task id. None when at capacity."""
    now = time.monotonic()
    with _TASKS_LOCK:
        _prune_locked(now)
        if _running_locked() >= MAX_CONCURRENT_TASKS:
            return None
        task_id = uuid.uuid4().hex[:12]
        _TASKS[task_id] = {"state": "running", "result": {}, "finished_at": 0.0}
    return task_id


def status(task_id: str) -> dict | None:
    """The /status payload for a task, or None if this dispatcher never saw it.

    The task table is private to this module so that the HTTP layer cannot read it
    without the lock — the reason this returns a finished snapshot rather than the
    live entry.
    """
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return None
        if task["state"] == "running":
            return {"state": "running", "task_id": task_id}
        return {"state": "done", "task_id": task_id, **task["result"]}


def run_and_record(task_id: str, spec: dict) -> dict:
    """Run the task and store its result so /status can answer for it afterwards."""
    result = run_to_completion(task_id, spec)
    _finish(task_id, result)
    return result


def run_in_background(task_id: str, spec: dict) -> None:
    threading.Thread(target=run_and_record, args=(task_id, spec), daemon=True).start()
