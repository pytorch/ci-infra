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
tasks.MAX_CONCURRENT_TASKS plus the namespace ResourceQuota — /run is unauthenticated,
so without a cap a caller loop would provision nodes until an AWS quota noticed.

The process is split by boundary, one file each, imports running strictly downward:

    __main__  -> http_api -> tasks -> kube

    kube      the Kubernetes API and the Job template — the task-pod security boundary
    tasks     the task table and the run-one-Job-to-completion loop
    http_api  parse, bound, dispatch, answer
    __main__  wiring, and nothing else

This file holds no logic on purpose: everything it could plausibly decide is a decision
that belongs to one of the files above, where it has a test.
"""

from __future__ import annotations

import os

import http_api
import kube

PORT = int(os.environ.get("PORT", "8080"))


def main() -> None:
    server = http_api.HTTPServerV6(("::", PORT), http_api.Handler)
    print(
        f"[sandbox-dispatcher] listening on [::]:{PORT}, namespace={kube.NAMESPACE}, image={kube.AGENT_IMAGE}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
