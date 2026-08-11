#!/usr/bin/env python3
"""Sandbox agent HTTP worker.

A long-running worker — callable over the network like buildkitd (a runner does
`curl sandbox-agent.ai-sandbox.svc:8080/run ...`, no K8s RBAC, just a NetworkPolicy
allow). It runs under gVisor on the ai-sandbox fleet.

Credentials: the ONLY credential the agent holds is a read-only Bedrock IRSA role
(bound to its ServiceAccount). It clones PUBLIC repos anonymously (no token) and
calls Bedrock directly with boto3 (which uses the IRSA web-identity credentials).

Endpoints:
  GET  /healthz  -> {"status": "ok"}
  POST /run      -> body {"repo","ref","task","model"?}; returns
                    {"cloned": bool, "file_count": int, "top_level": [str],
                     "report": str, "errors": {...}}

Single worker, one task at a time (prototype).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
from botocore.exceptions import BotoCoreError, ClientError

PORT = int(os.environ.get("PORT", "8080"))
REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_MODEL = os.environ.get("BEDROCK_DEFAULT_MODEL_ID", "")
CLONE_TIMEOUT_S = 120


def clone_repo(repo: str, ref: str, dest: str) -> int:
    """Shallow, anonymous clone of a public repo. Returns the tracked-file count."""
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, f"https://github.com/{repo}.git", dest],
        check=True,
        capture_output=True,
        text=True,
        timeout=CLONE_TIMEOUT_S,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    listing = subprocess.run(
        ["git", "-C", dest, "ls-files"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return len([line for line in listing.stdout.splitlines() if line.strip()])


def top_level_entries(dest: str) -> list[str]:
    """Top-level tracked entries (`dir/` for trees). Grounding for the prompt: with
    only a file *count* the model invents a plausible listing, so the report says
    nothing about the repo the agent actually cloned."""
    listing = subprocess.run(
        ["git", "-C", dest, "ls-tree", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    names = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    return [f"{n}/" if os.path.isdir(os.path.join(dest, n)) else n for n in names]


def invoke_bedrock(model: str, prompt: str) -> str:
    """Call Bedrock InvokeModel directly via boto3 (signed with the pod's IRSA creds)."""
    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
    )
    client = boto3.client("bedrock-runtime", region_name=REGION)
    resp = client.invoke_model(modelId=model, body=body, contentType="application/json", accept="application/json")
    payload = json.loads(resp["body"].read())
    content = payload.get("content") or []
    return content[0]["text"] if content else ""


def build_prompt(repo: str, ref: str, task: str, file_count: int, entries: list[str]) -> str:
    """Prompt the model with what the agent actually observed in the clone, and
    tell it not to fill gaps — an ungrounded answer looks identical to a correct
    one, which would make the canary's 'Bedrock returned a report' assertion
    meaningless."""
    lines = [
        f"You are inspecting a checkout of {repo} at ref {ref}.",
        f"It has {file_count} tracked files.",
    ]
    if entries:
        lines += ["", "Top-level entries (complete list, `/` marks a directory):", *(f"  {e}" for e in entries)]
    lines += [
        "",
        f"Task: {task}",
        "",
        "Answer only from the listing above. If it doesn't contain the answer, say so "
        "instead of guessing — do not invent paths.",
    ]
    return "\n".join(lines)


def run_task(spec: dict) -> dict:
    """Clone the repo, then (optionally) ask Bedrock about it. Never raises —
    each stage's failure is captured so callers see exactly what worked."""
    repo = spec["repo"]
    ref = spec.get("ref", "main")
    task = spec.get("task", "Summarize this repository.")
    model = spec.get("model") or DEFAULT_MODEL
    result: dict = {"cloned": False, "file_count": 0, "top_level": [], "report": "", "errors": {}}

    with tempfile.TemporaryDirectory() as workdir:
        try:
            result["file_count"] = clone_repo(repo, ref, workdir)
            result["cloned"] = True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            result["errors"]["clone"] = getattr(exc, "stderr", None) or str(exc)
            return result

        if not model:
            result["errors"]["bedrock"] = "no model configured (set BEDROCK_DEFAULT_MODEL_ID or pass 'model')"
            return result

        try:
            entries = top_level_entries(workdir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # Grounding is best-effort — a repo we can't list is still worth asking about.
            result["errors"]["listing"] = getattr(exc, "stderr", None) or str(exc)
            entries = []
        result["top_level"] = entries

        prompt = build_prompt(repo, ref, task, result["file_count"], entries)
        try:
            result["report"] = invoke_bedrock(model, prompt)
        except (BotoCoreError, ClientError, KeyError, ValueError) as exc:
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
