#!/usr/bin/env python3
"""Sandbox agent HTTP worker.

A long-running, credential-free worker — callable over the network exactly like
buildkitd (a runner does `curl sandbox-agent.ai-sandbox.svc:8080/run ...`, no K8s
RBAC, just a NetworkPolicy allow). It runs under gVisor on the ai-sandbox fleet.

It holds NO credentials: it clones the target repo through the agent-vault proxy
(which injects a read-only GitHub token on the wire) and calls Bedrock through
the sigv4 proxy (which signs with its IRSA identity). This process never sees a
token.

Endpoints:
  GET  /healthz  -> {"status": "ok"}
  POST /run      -> body {"repo","ref","task","model"?}; returns
                    {"cloned": bool, "file_count": int, "report": str, "errors": {...}}

Single worker, one task at a time (prototype). stdlib only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8080"))
REGION = os.environ.get("AWS_REGION", "us-east-1")
SIGV4_PROXY = os.environ.get("SIGV4_PROXY", "sigv4-proxy.ai-sandbox.svc.cluster.local:8080")
DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL_ID", "")
CLONE_TIMEOUT_S = 120
BEDROCK_TIMEOUT_S = 120


def clone_repo(repo: str, ref: str, dest: str) -> int:
    """Shallow-clone through the agent-vault proxy (env: HTTPS_PROXY, GIT_SSL_CAINFO).

    Returns the tracked-file count. Raises on failure.
    """
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, f"https://github.com/{repo}.git", dest],
        check=True,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_S,
    )
    listing = subprocess.run(
        ["git", "-C", dest, "ls-files"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return len([line for line in listing.stdout.splitlines() if line.strip()])


def invoke_bedrock(model: str, prompt: str) -> str:
    """Call Bedrock InvokeModel through the sigv4 proxy (unsigned in, signed out)."""
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
    ).encode()
    req = urllib.request.Request(
        f"http://{SIGV4_PROXY}/model/{model}/invoke",
        data=body,
        method="POST",
        headers={
            "Host": f"bedrock-runtime.{REGION}.amazonaws.com",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=BEDROCK_TIMEOUT_S) as resp:  # noqa: S310
        payload = json.loads(resp.read())
    content = payload.get("content") or []
    return content[0]["text"] if content else ""


def run_task(spec: dict) -> dict:
    """Clone the repo, then (optionally) ask Bedrock about it. Never raises —
    each stage's failure is captured so callers see exactly what worked."""
    repo = spec["repo"]
    ref = spec.get("ref", "main")
    task = spec.get("task", "Summarize this repository.")
    model = spec.get("model") or DEFAULT_MODEL
    result: dict = {"cloned": False, "file_count": 0, "report": "", "errors": {}}

    with tempfile.TemporaryDirectory() as workdir:
        try:
            result["file_count"] = clone_repo(repo, ref, workdir)
            result["cloned"] = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            result["errors"]["clone"] = getattr(exc, "stderr", None) or str(exc)
            return result

        if not model:
            result["errors"]["bedrock"] = "no model configured (set BEDROCK_MODEL_ID or pass 'model')"
            return result

        prompt = f"Task: {task}\n\nRepository {repo}@{ref} has {result['file_count']} tracked files."
        try:
            result["report"] = invoke_bedrock(model, prompt)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            result["errors"]["bedrock"] = str(exc)

    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            spec = json.loads(self.rfile.read(length) or "{}")
            if not spec.get("repo"):
                raise ValueError("missing 'repo'")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, run_task(spec))

    def log_message(self, fmt: str, *args) -> None:
        print(f"[sandbox-agent] {fmt % args}")


class HTTPServerV6(HTTPServer):
    # OSDC EKS is IPv6-only: the pod IP (and the readiness probe / Service target)
    # are IPv6, so the listener must bind :: — a default AF_INET server binds
    # 0.0.0.0 and is unreachable on this cluster.
    address_family = socket.AF_INET6


def main() -> None:
    server = HTTPServerV6(("::", PORT), Handler)
    print(f"[sandbox-agent] listening on [::]:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
