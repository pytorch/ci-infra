"""Everything that talks to the Kubernetes API: the SandboxClaim each task runs in.

This is the bottom of the dispatcher: it imports no other module here, so the task
lifecycle above it can be read without knowing how a claim is spelled, and this file can
be reviewed as one thing — the security boundary for every task pod.

Access is stdlib urllib against the in-cluster API with the projected SA token, the same
shape as base/kubernetes/node-taint-remover. RBAC is namespaced to sandboxclaims in
ai-sandbox.

A task's pod is the `sandbox-task` SandboxTemplate (kubernetes/base/sandbox-template.yaml);
a claim only names its warm pool and an expiry, and the task itself is started in the pod
through sandboxd (sandboxd.py).
"""

from __future__ import annotations

import datetime
import functools
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import effects

TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

NAMESPACE = os.environ.get("NAMESPACE", "ai-sandbox")
CLAIMS_PATH = f"/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/{NAMESPACE}/sandboxclaims"
# The SandboxWarmPool (and SandboxTemplate) every claim draws from.
WARM_POOL = "sandbox-task"
# Where the git credential proxy listens. dispatcher.yaml sets it; empty disables the
# proxy path entirely.
GIT_PROXY = os.environ.get("GIT_PROXY", "")
# The repositories that NEED the proxy, because they are private and an anonymous fetch
# of them 404s. Code in git, like the manifests: it is the list of repos whose contents
# leave the cluster under our credential. Deployment topology, not policy — a manifest
# decides WHETHER a caller may read a repo, this decides HOW the pod reaches it
# (test_authorize checks every private repo a manifest grants is listed here).
#
# Opt-in per repo rather than "proxy everything". Sending public clones through it would
# make the proxy a hard dependency of every task — one missing Secret and nothing runs —
# and would push a 250 MB shallow clone of pytorch/pytorch through two nginx pods for no
# benefit. It must agree with the allowlist in kubernetes/base/git-proxy.yaml: a repo
# here but not there fetches and gets 403.
PRIVATE_REPOS = frozenset({"pytorch/ciforge"})

# Covers pod scheduling (including a Karpenter node when the fleet is full or cold), the
# fetch and diff (each git step bounded at 120s in the task image) and the agent loop
# (at most 600s). The loop does not assume the rest was quick: the task gets its
# deadline as SANDBOX_DEADLINE and ends the loop early enough to report. The template's
# sandboxd --execute-max-time-limit is the same number.
TASK_DEADLINE_S = int(os.environ.get("TASK_DEADLINE_S", "900"))
# A claim is deleted as soon as its task returns; its shutdownTime, this long past the
# deadline, is the backstop for a dispatcher that died mid-task. Short on purpose: a
# pending pod has no deadline of its own, so this is what bounds an orphan's quota.
CLAIM_GRACE_S = 60
# Claim Ready=False reasons that waiting cannot fix: fail the task instead of the deadline.
PERMANENT_CLAIM_FAILURES = frozenset({"TemplateNotFound", "WarmPoolNotFound"})

# The most a task may print (its stdout cap on the sandboxd call), and the most read from
# any API response.
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


def api_request(method: str, path: str, body: dict | None = None, content_type: str = ""):
    """One API call. Returns parsed JSON.

    `content_type` exists for PATCH, which the API server rejects as application/json —
    it wants to be told which patch dialect the body is in.
    """
    url = f"{_k8s_api()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {_read_token()}"}
    if data:
        headers["Content-Type"] = content_type or "application/json"
    headers["Accept"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=30) as resp:  # noqa: S310
            payload = resp.read(MAX_LOG_BYTES)
    except urllib.error.HTTPError as exc:
        detail = exc.read(MAX_ERROR_BYTES).decode(errors="replace")[:ERROR_QUOTE_CHARS] if hasattr(exc, "read") else ""
        raise ApiError(f"{method} {path} -> {exc.code}: {detail}") from None
    try:
        return json.loads(payload or b"{}")
    except ValueError as exc:
        # ValueError is neither ApiError nor OSError, so it would slip past every
        # caller's except clause, escape do_POST, and close the connection with no
        # response — the one answer this endpoint exists to avoid. Reachable through the
        # MAX_LOG_BYTES cap: a response past it is truncated into invalid JSON.
        raise ApiError(f"{method} {path} -> unparseable response: {exc}") from None


def effects_env(grant) -> str:
    """The writes this run may propose, as JSON for the task: it offers the agent a
    propose_effect tool only when this is non-empty. Informational for the pod — the
    dispatcher re-checks every proposal against the Grant when the task returns."""
    # max_bytes is the room left for the agent's text: screening puts the provenance line
    # in front of every body and counts it against the manifest's limit, so a proposal
    # the tool accepted at the full limit would be dropped after the run.
    head = len(effects.attribution("0" * 40, grant.manifest).encode())
    return json.dumps(
        [
            {
                "effect": e.kind,
                "max_bytes": max(0, e.max_bytes - head),
                **({"conclusions": sorted(e.conclusions)} if e.conclusions else {}),
            }
            for e in grant.effects
        ]
    )


def task_env(grant, deadline: int) -> dict[str, str]:
    """Every per-task input task.py reads, from the Grant alone — never the request body.

    One place, so nothing that starts the task can forget an input.

    It reads one thing besides the Grant — `PRIVATE_REPOS`, to decide whether to hand the
    task the proxy's address. That is deployment topology ("which repos need a credential
    on the way out"), not authorization: the answer is a function of `grant.clone_repo`,
    which the caller may choose only among the repositories its manifest allows, so
    nothing a caller sends can route a fetch the manifest did not grant.

    `deadline` is epoch seconds when the task will be stopped; the agent loop ends early
    enough to report before it.
    """
    return {
        # Only for a repo that needs a credential — see PRIVATE_REPOS. A public clone goes
        # straight to github.com, so the proxy being down or unconfigured cannot break it.
        "GIT_PROXY": GIT_PROXY if grant.clone_repo in PRIVATE_REPOS else "",
        "SANDBOX_REPO": grant.clone_repo,
        "SANDBOX_REF": grant.ref,
        "SANDBOX_BASE": grant.base,
        "SANDBOX_EFFECTS": effects_env(grant),
        "SANDBOX_TASK": grant.task,
        "SANDBOX_MODEL": grant.model,
        # Empty rather than "0" when there is no review, so task.py's "skip the empty ones"
        # rule drops it and the agent sees no `pr` key at all — the same shape a
        # pre-review dispatcher produced.
        "SANDBOX_PR": str(grant.pr) if grant.pr else "",
        "SANDBOX_DEADLINE": str(deadline),
    }


def last_json_object(text: str, task_id: str) -> dict:
    """The result a task printed: the last line of stdout that parses as a JSON object,
    so a stray log line before it is harmless."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise ApiError(f"sandbox-task-{task_id} printed no result JSON")


def claim_manifest(task_id: str, now: datetime.datetime | None = None) -> dict:
    """The SandboxClaim for one task: the pool and an expiry, nothing about the task.

    The task's inputs go to the pod over the sandboxd call, never into the pod spec, so a
    pod the controller replaces has nothing in it to run again. admissionpolicy.yaml
    holds the dispatcher to exactly this shape.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    shutdown = now + datetime.timedelta(seconds=TASK_DEADLINE_S + CLAIM_GRACE_S)
    return {
        "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
        "kind": "SandboxClaim",
        "metadata": {
            "name": f"sandbox-task-{task_id}",
            "namespace": NAMESPACE,
            "labels": {"osdc.io/module": "agent-sandbox"},
        },
        "spec": {
            "warmPoolRef": {"name": WARM_POOL},
            "lifecycle": {"shutdownTime": shutdown.strftime("%Y-%m-%dT%H:%M:%SZ"), "shutdownPolicy": "Delete"},
        },
    }


def create_claim(task_id: str) -> None:
    api_request("POST", CLAIMS_PATH, body=claim_manifest(task_id))


def claim_state(task_id: str) -> tuple[str, str]:
    """('ready', pod IP), ('pending', ''), or ('failed', why).

    Ready=True with a pod IP means the pod is up and passed its sandboxd readiness probe.
    Finished means the pod exited, which before the task starts is a dead sandboxd.
    """
    claim = api_request("GET", f"{CLAIMS_PATH}/sandbox-task-{task_id}")
    status = claim.get("status") or {}
    conditions = {c.get("type"): c for c in status.get("conditions") or []}
    finished = conditions.get("Finished") or {}
    if finished.get("status") == "True":
        return "failed", f"the sandbox pod exited ({finished.get('reason') or 'no reason given'})"
    ready = conditions.get("Ready") or {}
    pod_ips = (status.get("sandbox") or {}).get("podIPs") or []
    if ready.get("status") == "True" and pod_ips:
        return "ready", pod_ips[0]
    if ready.get("status") == "False" and ready.get("reason") in PERMANENT_CLAIM_FAILURES:
        return "failed", f"{ready['reason']}: {ready.get('message', '')}"
    return "pending", ""


def delete_claim(task_id: str) -> None:
    """Delete the claim, and with it the Sandbox and its pod. Not fatal — shutdownTime is the backstop."""
    try:
        api_request("DELETE", f"{CLAIMS_PATH}/sandbox-task-{task_id}", body={"propagationPolicy": "Background"})
    except (ApiError, OSError) as exc:
        print(f"[sandbox-dispatcher] delete sandbox-task-{task_id} failed (shutdownTime will collect it): {exc}")
