#!/usr/bin/env python3
"""One sandbox task, then exit. The entrypoint of every task pod.

The dispatcher creates one Job per request and passes the spec as env vars; this
prints the result as a single line of JSON on stdout, which the dispatcher reads back
out of the pod's log. Nothing here listens on a socket and nothing is shared between
tasks — a fresh pod per request IS the isolation reset.

Exit status is 0 whenever a result was produced, including a result whose `errors`
describe a failed clone or a refused Bedrock call: those are answers, not crashes.
A non-zero exit means no JSON was printed, which the dispatcher reports as a pod
failure.
"""

from __future__ import annotations

import json
import os
import sys

from sandbox import run_task

# Env var -> spec key. The dispatcher validates types before it ever creates the Job,
# so anything arriving here is a string; run_task falls back to its own defaults for
# the empty ones.
SPEC_ENV = {
    "SANDBOX_REPO": "repo",
    "SANDBOX_REF": "ref",
    "SANDBOX_TASK": "task",
    "SANDBOX_MODEL": "model",
}


def spec_from_env(env: dict[str, str]) -> dict:
    """Build a run_task spec from the environment. Raises KeyError without a repo."""
    spec = {key: env[name] for name, key in SPEC_ENV.items() if env.get(name)}
    if not spec.get("repo"):
        raise KeyError("SANDBOX_REPO is required")
    return spec


def main() -> int:
    try:
        spec = spec_from_env(dict(os.environ))
    except KeyError as exc:
        # No spec means the Job was built wrong — a dispatcher bug, not a task result.
        print(f"[sandbox-task] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(run_task(spec)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
