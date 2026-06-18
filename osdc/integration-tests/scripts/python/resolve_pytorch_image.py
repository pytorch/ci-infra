"""Resolve the pytorch CI image .ci/docker tree-SHA via the GitHub API.

Exports a single function: `resolve_ci_docker_hash(ref="main") -> str`.
Callers compose the full ECR image tag themselves (e.g. `f"{name}-{sha}"`).
"""

import json
import subprocess


def resolve_ci_docker_hash(ref: str = "main") -> str:
    """Return the tree-SHA of pytorch/pytorch:.ci/docker at the given ref."""
    endpoint = f"repos/pytorch/pytorch/git/trees/{ref}:.ci/docker"
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api {endpoint} timed out after 30s") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"gh api {endpoint} failed (exit {exc.returncode}): {exc.stderr.strip()}"
        ) from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api {endpoint} returned invalid JSON: {exc}") from exc

    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not sha:
        raise RuntimeError(
            f"gh api {endpoint} response missing 'sha' field: {payload!r}"
        )
    return sha
