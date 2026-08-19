#!/usr/bin/env python3
"""Sandbox dispatcher: one Job per request, so tasks run in parallel.

This is the trusted side of the sandbox and the only thing callers address
(`sandbox-agent.ai-sandbox.svc:8080`, reachable from arc-runners like buildkitd). It
never clones, never prompts a model and holds no AWS identity — it turns a request
into a Kubernetes Job running the task image under gVisor, waits for it, and reads the
result back out of the pod's log.

Why a Job per request rather than a pool of warm workers: parallelism comes from
Karpenter (a pending task pod adds an ai-sandbox node) instead of from replica count
plus a connection-aware load balancer, and each task gets a fresh pod, which is the
isolation reset a shared worker cannot give. The bound on all of it is
MAX_CONCURRENT_TASKS here plus the namespace ResourceQuota — /run is unauthenticated,
so without a cap a caller loop would provision nodes until an AWS quota noticed.

Endpoints:
  GET  /healthz      -> {"status": "ok"}
  POST /run          -> body {"repo","ref","task","model"?,"wait"?}
                        wait=true (default): blocks, returns the task result
                        wait=false: returns {"task_id": ...} immediately
  GET  /status/<id>  -> {"state": "running"|"done", ...result}

Kubernetes access is stdlib urllib against the in-cluster API with the projected SA
token, the same shape as base/kubernetes/node-taint-remover. RBAC is namespaced to
jobs + pods + pods/log in ai-sandbox.
"""

from __future__ import annotations

import functools
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

PORT = int(os.environ.get("PORT", "8080"))
NAMESPACE = os.environ.get("NAMESPACE", "ai-sandbox")
# Substituted by deploy.sh: the content-hash tag of the task image it just built.
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
SIGV4_PROXY = os.environ.get("SIGV4_PROXY", "sigv4-proxy.ai-sandbox.svc.cluster.local:8080")
DEFAULT_MODEL = os.environ.get("BEDROCK_DEFAULT_MODEL_ID", "")

# Per replica. The namespace ResourceQuota is the cluster-wide bound — this exists so a
# caller gets a clean 429 instead of a wall of Jobs the quota then rejects one by one.
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "6"))
# A task is a clone plus an invoke, each bounded at 120s in the task image, plus pod
# scheduling — which includes a Karpenter node when the fleet is full or cold.
TASK_DEADLINE_S = int(os.environ.get("TASK_DEADLINE_S", "900"))
# Finished Jobs are deleted as soon as their log is read; this is the backstop for a
# dispatcher that died mid-wait, and has to outlast a plausible restart.
JOB_TTL_S = 3600
# How long /status can still answer for a finished task before its result is dropped.
RESULT_RETENTION_S = int(os.environ.get("RESULT_RETENTION_S", "3600"))
POLL_INTERVAL_S = 2
SLOT_CPU = os.environ.get("TASK_CPU", "2")
SLOT_MEMORY = os.environ.get("TASK_MEMORY", "4Gi")
SLOT_DISK = os.environ.get("TASK_EPHEMERAL_STORAGE", "20Gi")

# Request-surface bounds. /run is unauthenticated behind a NetworkPolicy, so the body
# is read under a size cap and the connection under a socket timeout.
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 30
MAX_LOG_BYTES = 1024 * 1024

TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


class ApiError(RuntimeError):
    """The API server answered non-2xx."""


def _k8s_api() -> str:
    """API base URL from in-cluster env vars (IPv6-safe — OSDC EKS is IPv6-only)."""
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise RuntimeError("KUBERNETES_SERVICE_HOST not set — not running inside a pod?")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}:{port}"


def _read_token() -> str:
    if not TOKEN_PATH.exists():
        raise RuntimeError(f"ServiceAccount token not found at {TOKEN_PATH}")
    return TOKEN_PATH.read_text().strip()


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Built once: every call would otherwise re-read and re-parse the CA bundle, and
    each in-flight task polls every POLL_INTERVAL_S.

    The token above is deliberately NOT cached with it — the projected one rotates, so a
    cached copy starts failing every call about an hour after startup.
    """
    ctx = ssl.create_default_context()
    if CA_PATH.exists():
        ctx.load_verify_locations(cafile=str(CA_PATH))
    return ctx


def api_request(method: str, path: str, body: dict | None = None, raw: bool = False):
    """One API call. Returns parsed JSON, or text when raw (pod logs are not JSON)."""
    url = f"{_k8s_api()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {_read_token()}"}
    if data:
        headers["Content-Type"] = "application/json"
    if not raw:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:  # noqa: S310
            payload = resp.read(MAX_LOG_BYTES)
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_BODY_BYTES).decode(errors="replace")[:500] if hasattr(exc, "read") else ""
        raise ApiError(f"{method} {path} -> {exc.code}: {detail}") from None
    if raw:
        return payload.decode(errors="replace")
    try:
        return json.loads(payload or b"{}")
    except ValueError as exc:
        # ValueError is neither ApiError nor OSError, so it would slip past every
        # caller's except clause, escape do_POST, and close the connection with no
        # response — the one answer this endpoint exists to avoid. Reachable through the
        # MAX_LOG_BYTES cap: a pod list past it is truncated into invalid JSON.
        raise ApiError(f"{method} {path} -> unparseable response: {exc}") from None


def job_manifest(task_id: str, spec: dict) -> dict:
    """The Job for one task: the task image under gVisor, with no identity.

    backoffLimit 0 on purpose — a retry would clone and prompt the model a second time
    and bill for it, and the result object already carries per-stage errors, so a
    failure here is worth surfacing rather than repeating.
    """
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"sandbox-task-{task_id}",
            "namespace": NAMESPACE,
            "labels": {"app": "sandbox-task", "osdc.io/module": "agent-sandbox"},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": TASK_DEADLINE_S,
            "ttlSecondsAfterFinished": JOB_TTL_S,
            "template": {
                "metadata": {"labels": {"app": "sandbox-task", "osdc.io/module": "agent-sandbox"}},
                "spec": {
                    "restartPolicy": "Never",
                    # gvisor pins the pod to the ai-sandbox fleet and runs it under
                    # runsc; the SA has no RBAC and no token mounted.
                    "runtimeClassName": "gvisor",
                    "serviceAccountName": "sandbox-agent",
                    "automountServiceAccountToken": False,
                    "containers": [
                        {
                            "name": "task",
                            "image": AGENT_IMAGE,
                            "env": [
                                {"name": "PYTHONUNBUFFERED", "value": "1"},
                                {"name": "AWS_REGION", "value": REGION},
                                {"name": "SIGV4_PROXY", "value": SIGV4_PROXY},
                                {"name": "BEDROCK_DEFAULT_MODEL_ID", "value": DEFAULT_MODEL},
                                {"name": "SANDBOX_REPO", "value": spec["repo"]},
                                {"name": "SANDBOX_REF", "value": spec.get("ref", "")},
                                {"name": "SANDBOX_TASK", "value": spec.get("task", "")},
                                {"name": "SANDBOX_MODEL", "value": spec.get("model", "")},
                            ],
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "allowPrivilegeEscalation": False,
                            },
                            # requests == limits (Guaranteed QoS): fleet capacity stays
                            # a division, not a guess, and disk is the dimension the
                            # caller picks — an uncapped clone evicts its own pod
                            # instead of pushing the node into DiskPressure.
                            "resources": {
                                "requests": {
                                    "cpu": SLOT_CPU,
                                    "memory": SLOT_MEMORY,
                                    "ephemeral-storage": SLOT_DISK,
                                },
                                "limits": {
                                    "cpu": SLOT_CPU,
                                    "memory": SLOT_MEMORY,
                                    "ephemeral-storage": SLOT_DISK,
                                },
                            },
                        }
                    ],
                },
            },
        },
    }


def create_job(task_id: str, spec: dict) -> None:
    api_request("POST", f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs", body=job_manifest(task_id, spec))


def job_state(task_id: str) -> tuple[str, str]:
    """(state, detail) for a Job: 'running', 'succeeded' or 'failed'."""
    job = api_request("GET", f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/sandbox-task-{task_id}")
    status = job.get("status", {})
    if status.get("succeeded"):
        return "succeeded", ""
    conditions = status.get("conditions") or []
    for cond in conditions:
        if cond.get("type") == "Failed" and cond.get("status") == "True":
            return "failed", cond.get("reason") or cond.get("message") or "Job failed"
    return "running", ""


def task_result(task_id: str) -> dict:
    """Parse the result the task printed. Its log IS the transport.

    The last JSON object on stdout wins, so a stray log line before it is harmless.
    """
    pods = api_request(
        "GET",
        f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=job-name%3Dsandbox-task-{task_id}",
    )
    items = pods.get("items") or []
    if not items:
        raise ApiError(f"no pod found for sandbox-task-{task_id}")
    name = items[-1]["metadata"]["name"]
    log = api_request("GET", f"/api/v1/namespaces/{NAMESPACE}/pods/{name}/log", raw=True)
    for line in reversed(log.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise ApiError(f"sandbox-task-{task_id} printed no result JSON")


def delete_job(task_id: str) -> None:
    """Delete the Job and its pod. Not fatal — ttlSecondsAfterFinished is the backstop."""
    try:
        api_request(
            "DELETE",
            f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/sandbox-task-{task_id}",
            body={"propagationPolicy": "Background"},
        )
    except (ApiError, OSError) as exc:
        print(f"[sandbox-dispatcher] delete sandbox-task-{task_id} failed (TTL will collect it): {exc}")


def run_to_completion(task_id: str, spec: dict) -> dict:
    """Create the Job, wait for it, return the result. Never raises."""
    # Grace beyond the Job's own activeDeadlineSeconds, which is the same number: without
    # it both fire at once and this loop reports a flat "did not finish" instead of the
    # Job's DeadlineExceeded, which at least names what was still running.
    deadline = time.monotonic() + TASK_DEADLINE_S + POLL_INTERVAL_S * 3
    try:
        create_job(task_id, spec)
    except (ApiError, OSError) as exc:
        return {"errors": {"dispatch": str(exc)}}

    try:
        while True:
            if time.monotonic() > deadline:
                return {"errors": {"dispatch": f"task did not finish within {TASK_DEADLINE_S}s"}}
            state, detail = job_state(task_id)
            if state == "succeeded":
                return task_result(task_id)
            if state == "failed":
                # A pod that died before printing (OOM, eviction, image pull) has no
                # result to parse; say so rather than reporting an empty one.
                try:
                    return task_result(task_id)
                except ApiError:
                    return {"errors": {"task": f"pod failed: {detail}"}}
            time.sleep(POLL_INTERVAL_S)
    except (ApiError, OSError) as exc:
        return {"errors": {"dispatch": str(exc)}}
    finally:
        delete_job(task_id)


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


def _run_in_background(task_id: str, spec: dict) -> None:
    _finish(task_id, run_to_completion(task_id, spec))


class Handler(BaseHTTPRequestHandler):
    # Whole-connection socket timeout (socketserver applies it in setup()). Without it
    # a caller that announces a body and sends it slowly, or never, parks a handler
    # thread for as long as it likes, and ThreadingHTTPServer caps nothing. A waiting
    # /run sits between socket operations, so it never trips this.
    timeout = REQUEST_TIMEOUT_S

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            # in_flight/capacity are here because a 429 is otherwise indistinguishable
            # from a wedged dispatcher: this says which one it is without kubectl.
            self._send(200, {"status": "ok", "in_flight": slots_in_use(), "capacity": MAX_CONCURRENT_TASKS})
            return
        if self.path.startswith("/status/"):
            task_id = self.path[len("/status/") :]
            if not TASK_ID_RE.match(task_id):
                self._send(400, {"error": "malformed task id"})
                return
            with _TASKS_LOCK:
                task = _TASKS.get(task_id)
            if task is None:
                self._send(404, {"error": "unknown task id"})
                return
            if task["state"] == "running":
                self._send(200, {"state": "running", "task_id": task_id})
            else:
                self._send(200, {"state": "done", "task_id": task_id, **task["result"]})
            return
        self._send(404, {"error": "not found"})

    def _read_spec(self) -> dict:
        """Parse and validate the /run body, raising ValueError with the reason.

        Bounded, because the declared length is caller-controlled and read(-1) would
        read until end of file. Types are checked rather than truth-tested: a field of
        the wrong type reaches git or the proxy as an argument.
        """
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError(f"invalid Content-Length: {raw_length!r}") from None
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError(f"Content-Length must be between 0 and {MAX_BODY_BYTES}, got {length}")

        spec = json.loads(self.rfile.read(length) or "{}")
        if not isinstance(spec, dict):
            raise ValueError("body must be a JSON object")
        if not isinstance(spec.get("repo"), str) or not spec["repo"]:
            raise ValueError("missing 'repo'")
        for key in ("ref", "task", "model"):
            if key in spec and not isinstance(spec[key], str):
                raise ValueError(f"'{key}' must be a string")
        if "wait" in spec and not isinstance(spec["wait"], bool):
            raise ValueError("'wait' must be a boolean")
        return spec

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        try:
            spec = self._read_spec()
        except ValueError as exc:
            # json.JSONDecodeError is a ValueError subclass, so a malformed body, a
            # non-object body and a bad header all answer 400 rather than dropping the
            # connection with no response at all.
            self._send(400, {"error": str(exc)})
            return
        if not AGENT_IMAGE:
            self._send(500, {"error": "AGENT_IMAGE not set — deploy.sh did not substitute the task image"})
            return

        task_id = start_task()
        if task_id is None:
            self._send(429, {"error": f"at capacity: {MAX_CONCURRENT_TASKS} tasks in flight"})
            return

        if spec.get("wait", True):
            result = run_to_completion(task_id, spec)
            _finish(task_id, result)
            self._send(200, {"task_id": task_id, **result})
            return

        threading.Thread(target=_run_in_background, args=(task_id, spec), daemon=True).start()
        self._send(202, {"task_id": task_id, "state": "running"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[sandbox-dispatcher] {fmt % args}")


class HTTPServerV6(ThreadingHTTPServer):
    # OSDC EKS is IPv6-only: the pod IP (and the readiness probe / Service target) are
    # IPv6, so the listener must bind :: — a default AF_INET server binds 0.0.0.0 and
    # is unreachable on this cluster.
    #
    # Threading, because a synchronous /run holds its connection for the length of a
    # task: without it one caller would block /healthz, the readiness probe would time
    # out, and the pod would be dropped from the Service mid-task.
    address_family = socket.AF_INET6


def main() -> None:
    server = HTTPServerV6(("::", PORT), Handler)
    print(f"[sandbox-dispatcher] listening on [::]:{PORT}, namespace={NAMESPACE}, image={AGENT_IMAGE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
