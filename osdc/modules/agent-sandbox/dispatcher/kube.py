"""Everything that talks to the Kubernetes API, and the Job template it sends.

This is the bottom of the dispatcher: it imports no other module here, so the task
lifecycle above it can be read without knowing how a Job is spelled, and this file can
be reviewed as one thing — the security boundary for every task pod.

Access is stdlib urllib against the in-cluster API with the projected SA token, the same
shape as base/kubernetes/node-taint-remover. RBAC is namespaced to jobs + pods + pods/log
in ai-sandbox.
"""

from __future__ import annotations

import functools
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

NAMESPACE = os.environ.get("NAMESPACE", "ai-sandbox")
# Substituted by deploy.sh: the content-hash tag of the task image it just built.
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
SIGV4_PROXY = os.environ.get("SIGV4_PROXY", "sigv4-proxy.ai-sandbox.svc.cluster.local:8080")
DEFAULT_MODEL = os.environ.get("BEDROCK_DEFAULT_MODEL_ID", "")

# A task is a clone plus an invoke, each bounded at 120s in the task image, plus pod
# scheduling — which includes a Karpenter node when the fleet is full or cold.
TASK_DEADLINE_S = int(os.environ.get("TASK_DEADLINE_S", "900"))
# Finished Jobs are deleted as soon as their log is read; this is the backstop for a
# dispatcher that died mid-wait, and has to outlast a plausible restart.
JOB_TTL_S = 3600
SLOT_CPU = os.environ.get("TASK_CPU", "2")
SLOT_MEMORY = os.environ.get("TASK_MEMORY", "4Gi")
SLOT_DISK = os.environ.get("TASK_EPHEMERAL_STORAGE", "20Gi")

MAX_LOG_BYTES = 1024 * 1024
# How much of a non-2xx body to READ, not how much of it survives into the error: the
# quote itself is bounded by ERROR_QUOTE_CHARS below. Same number as the request-body cap
# in http_api, and deliberately a separate constant: this one bounds what we read FROM the
# API server, that one bounds what a caller may send us.
MAX_ERROR_BYTES = 64 * 1024
# How much of what was read reaches the ApiError message. Named rather than sliced inline
# so both bounds on an error body are stated in the same place.
ERROR_QUOTE_CHARS = 500


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


def api_request(method: str, path: str, body: dict | None = None, raw: bool = False, content_type: str = ""):
    """One API call. Returns parsed JSON, or text when raw (pod logs are not JSON).

    `content_type` exists for PATCH, which the API server rejects as application/json —
    it wants to be told which patch dialect the body is in.
    """
    url = f"{_k8s_api()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {_read_token()}"}
    if data:
        headers["Content-Type"] = content_type or "application/json"
    if not raw:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:  # noqa: S310
            payload = resp.read(MAX_LOG_BYTES)
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_ERROR_BYTES).decode(errors="replace")[:ERROR_QUOTE_CHARS] if hasattr(exc, "read") else ""
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


def job_manifest(task_id: str, grant) -> dict:
    """The Job for one task: the task image under gVisor, with no identity.

    Takes a `Grant`, never a request body, and that signature is the layering rule made
    unavoidable: by the time execution reaches this function every decision has been
    made, and there is nothing here to make one from. A future capability manifest
    changes where the Grant's values come from and leaves this function untouched.

    Every isolation property of a task pod is decided here, which is why
    kubernetes/base/admissionpolicy.yaml restates the same contract in the API server:
    a bug in this function is otherwise the whole of the sandbox failing open.

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
                                {"name": "SANDBOX_REPO", "value": grant.clone_repo},
                                {"name": "SANDBOX_REF", "value": grant.ref},
                                {"name": "SANDBOX_TASK", "value": grant.task},
                                {"name": "SANDBOX_MODEL", "value": grant.model},
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


def create_job(task_id: str, grant) -> None:
    api_request("POST", f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs", body=job_manifest(task_id, grant))


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
