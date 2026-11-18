"""The task lifecycle: reserve a slot, run one task to completion, keep the result.

Sits between the HTTP surface and kube.py / sandboxd.py. It owns the only mutable state
in the process — the in-memory task table — and it is the one place that knows a task is
"claim a sandbox, wait for it, run task.py in it through sandboxd, delete the claim".

It calls kube and sandboxd through their modules (`kube.claim_state(...)`, not `from kube
import claim_state`) on purpose: a name imported at module load is a second binding that
a test patching `kube.claim_state` would not reach, and this file's tests exist to
substitute those calls.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import effects
import kube
import sandboxd

# Per replica. The namespace ResourceQuota is the cluster-wide bound — this exists so a
# caller gets a clean 429 instead of a wall of claims the quota then rejects one by one.
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "6"))
# How long /status can still answer for a finished task before its result is dropped.
RESULT_RETENTION_S = int(os.environ.get("RESULT_RETENTION_S", "3600"))
POLL_INTERVAL_S = 2
# What sandboxd starts in the pod, and where. The task's inputs travel as env on the call.
TASK_COMMAND = ["python3", "/usr/local/bin/task.py"]
TASK_CWD = "/workspace"
STDERR_LIMIT = 64 * 1024
# How much of stderr a failure reports.
STDERR_QUOTE_CHARS = 500
# Beyond the task's own time limit: sandboxd waits up to 10s to clean up a timed-out task.
RPC_GRACE_S = 15

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


def _run_to_completion(task_id: str, grant) -> dict:
    """Claim a sandbox, run the task in it, return the result. Never raises.

    Underscored because it does not release the slot start_task() reserved; going through
    run_and_record() is what does. A caller that paired start_task() with this one would
    leave an entry "running" forever, which _prune_locked() never drops and
    _running_locked() counts against MAX_CONCURRENT_TASKS for the life of the pod.

    One deadline covers both waiting for a pod (a cold one waits on Karpenter) and the
    run.
    """
    deadline = time.monotonic() + kube.TASK_DEADLINE_S
    try:
        kube.create_claim(task_id)
    except (kube.ApiError, OSError) as exc:
        return {"errors": {"dispatch": str(exc)}}

    try:
        pod_ip = None
        while pod_ip is None:
            if time.monotonic() > deadline:
                return {"errors": {"dispatch": f"no sandbox became ready within {kube.TASK_DEADLINE_S}s"}}
            state, detail = kube.claim_state(task_id)
            if state == "failed":
                return {"errors": {"dispatch": f"sandbox failed: {detail}"}}
            if state == "ready":
                pod_ip = detail
            else:
                time.sleep(POLL_INTERVAL_S)

        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            return {"errors": {"dispatch": f"task did not finish within {kube.TASK_DEADLINE_S}s"}}
        execution = sandboxd.execute(
            pod_ip,
            command=TASK_COMMAND,
            # Epoch seconds when sandboxd stops the task.
            env=kube.task_env(grant, int(time.time()) + remaining),
            cwd=TASK_CWD,
            time_limit_s=remaining,
            stdout_limit=kube.MAX_LOG_BYTES,
            stderr_limit=STDERR_LIMIT,
            rpc_timeout_s=remaining + RPC_GRACE_S,
        )
        return _result_of(task_id, execution)
    except (kube.ApiError, sandboxd.SandboxdError, OSError) as exc:
        return {"errors": {"dispatch": str(exc)}}
    finally:
        kube.delete_claim(task_id)


def _result_of(task_id: str, execution: sandboxd.Execution) -> dict:
    """What a finished run means. task.py exits 0 whenever it printed a result — including
    one whose `errors` describe a failed clone. A run that timed out or exited non-zero
    keeps whatever it printed, under the same rule as any failed run (_fail)."""
    if execution.timed_out:
        failure = f"task did not finish within {kube.TASK_DEADLINE_S}s"
    elif execution.exit_code:
        failure = f"task exited {execution.exit_code}"
    else:
        failure = ""
    try:
        result = kube.last_json_object(execution.stdout.decode(errors="replace"), task_id)
    except kube.ApiError:
        if execution.timed_out:
            return {"errors": {"dispatch": failure}}
        stderr = execution.stderr.decode(errors="replace").strip()[-STDERR_QUOTE_CHARS:]
        return {"errors": {"task": f"task exited {execution.exit_code} without a result: {stderr}"}}
    return _fail(result, failure) if failure else result


def _fail(result: dict, why: str) -> dict:
    """A result printed by a run that then failed. The failure is the dispatcher's fact,
    not the task's: it is recorded over whatever the task printed, and a failed run
    proposes nothing, because screening checks effects against the Grant, not against
    whether the run that proposed them finished."""
    errors = result.get("errors")
    result["errors"] = errors if isinstance(errors, dict) else {"result": errors}
    result["errors"]["dispatch"] = why
    result.pop("effects", None)
    return result


def _running_locked() -> int:
    """Tasks in flight. Callers hold _TASKS_LOCK, which is not reentrant, so this cannot
    go through slots_in_use()."""
    return sum(1 for t in _TASKS.values() if t["state"] == "running")


def _prune_locked(now: float) -> None:
    """Drop finished tasks past their retention. Callers hold _TASKS_LOCK.

    Results live in memory so /status can answer after the claim is gone, which means
    this dict is the one thing in a long-running dispatcher that grows without a bound
    of its own.
    """
    stale = [tid for tid, t in _TASKS.items() if t["state"] == "done" and now - t["finished_at"] > RESULT_RETENTION_S]
    for tid in stale:
        del _TASKS[tid]


def _finish(task_id: str, result: dict) -> None:
    with _TASKS_LOCK:
        # The owner is carried across rather than dropped: this entry replaces the
        # running one, and losing the field here would make every finished task
        # readable by any caller.
        owner = _TASKS.get(task_id, {}).get("owner", "")
        _TASKS[task_id] = {"state": "done", "result": result, "finished_at": time.monotonic(), "owner": owner}


def slots_in_use() -> int:
    with _TASKS_LOCK:
        return _running_locked()


def start_task(owner: str) -> str | None:
    """Reserve a slot and return its task id. None when at capacity or unable to mint one.

    `owner` is the Grant's caller. It is recorded now rather than derived later because
    /status must be able to refuse a caller asking about somebody else's task, and after
    the claim is deleted this table is the only place that answer exists.
    """
    now = time.monotonic()
    with _TASKS_LOCK:
        _prune_locked(now)
        if _running_locked() >= MAX_CONCURRENT_TASKS:
            return None
        # Retried rather than assumed unique. 12 hex characters is 48 bits, which makes a
        # collision vanishingly unlikely — but now that entries carry an owner, a reused
        # id would attribute one caller's result to another, and "vanishingly unlikely"
        # is a bad reason to leave that reachable at all.
        for _ in range(8):
            task_id = uuid.uuid4().hex[:12]
            if task_id not in _TASKS:
                _TASKS[task_id] = {"state": "running", "result": {}, "finished_at": 0.0, "owner": owner}
                return task_id
    return None


def status(task_id: str, owner: str) -> dict | None:
    """The /status payload for a task, or None if this caller may not see it.

    A task belonging to someone else is reported as absent, not as forbidden: telling a
    caller that an id exists but is not theirs turns this endpoint into a way to confirm
    that other callers are running tasks.

    The task table is private to this module so that the HTTP layer cannot read it
    without the lock — the reason this returns a finished snapshot rather than the
    live entry.
    """
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is None or task.get("owner") != owner:
            return None
        if task["state"] == "running":
            return {"state": "running", "task_id": task_id}
        # KNOWN GAP (deferred, see README § Limitations "A task can overwrite the
        # response fields the endpoints own"): task["result"] came from the task pod's
        # log, and spreading it LAST lets it overwrite "state" and "task_id" — a task
        # printing {"state": "running"} makes a finished task look unfinished to its own
        # caller. Reordering alone trades the bug for silently dropping a task's own
        # fields of those names, so this wants a response-schema decision (envelope vs
        # server-fields-last plus a caller audit), not a one-line swap here.
        return {"state": "done", "task_id": task_id, **task["result"]}


def run_and_record(task_id: str, grant) -> dict:
    """Run the task and store its result so /status can answer for it afterwards.

    KNOWN GAP (deferred, see README § Limitations "A slot can leak for the life of the
    pod"): the slot start_task() reserved is released only by reaching _finish() below,
    and nothing holds that if the runner raises. _run_to_completion() says it never
    raises and catches (ApiError, OSError), but kube._k8s_api() and kube._read_token()
    raise bare RuntimeError from OUTSIDE api_request()'s try block, so those escape —
    from its `finally: kube.delete_claim(...)` too, meaning a leaked slot does not imply
    the claim was never created. The entry then stays "running" forever: _prune_locked()
    only drops "done" ones and _running_locked() keeps counting it against
    MAX_CONCURRENT_TASKS.

    NOT simply a try/finally around _finish(): `result` is unbound on that path, so the
    fix has to decide what a crashed task records and whether the exception still
    propagates to the caller. Reachability is low, the loss is permanent.
    """
    # Proposed writes are screened against the Grant before anyone can read them back.
    result = effects.screen(_run_to_completion(task_id, grant), grant)
    _finish(task_id, result)
    return result


def run_in_background(task_id: str, grant) -> None:
    # KNOWN GAP, and a SEPARATE fix site from run_and_record()'s: if start() itself
    # fails (thread exhaustion, interpreter shutdown) the slot start_task() reserved is
    # already taken and no thread exists to release it. Cleanup has to live here or in
    # the caller, not in the runner.
    threading.Thread(target=run_and_record, args=(task_id, grant), daemon=True).start()
