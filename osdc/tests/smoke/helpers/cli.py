"""CLI wrappers for kubectl, helm, and AWS — with corporate-proxy bypass."""

from __future__ import annotations

import json
import os
import subprocess

DEFAULT_TIMEOUT = 90

__all__ = [
    "DEFAULT_TIMEOUT",
    "_proxy_bypass_env",
    "run_aws",
    "run_helm",
    "run_kubectl",
]


def _proxy_bypass_env() -> dict[str, str]:
    """Return an env dict that bypasses corporate proxy for AWS and EKS API calls.

    The Meta corporate proxy intercepts HTTPS connections, which causes
    kubectl/helm to fail with 'Unauthorized' when talking to EKS, and
    AWS CLI calls to fail for services like ECR, IAM, SQS, EventBridge.

    Bypasses:
      .amazonaws.com  — all AWS service endpoints (EC2, IAM, SQS, etc.)
      .eks.amazonaws.com — EKS Kubernetes API server (kubectl/helm)
    """
    env = os.environ.copy()
    suffixes = [".amazonaws.com", ".eks.amazonaws.com"]
    for key in ("NO_PROXY", "no_proxy"):
        current = env.get(key, "")
        for suffix in suffixes:
            if suffix not in current:
                current = f"{current},{suffix}" if current else suffix
        env[key] = current
    return env


def run_kubectl(
    args: list[str], namespace: str | None = None, timeout: int = DEFAULT_TIMEOUT, *, json_output: bool = True
) -> dict | str:
    """Run kubectl, optionally parse JSON output."""
    cmd = ["kubectl"]
    if namespace:
        cmd.extend(["-n", namespace])
    cmd.extend(args)
    if json_output:
        cmd.extend(["-o", "json"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=_proxy_bypass_env())
    if json_output:
        return json.loads(result.stdout)
    return result.stdout.strip()


def run_helm(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Run helm with -o json, return parsed output."""
    cmd = ["helm", *args, "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=_proxy_bypass_env())
    return json.loads(result.stdout)


def run_aws(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run aws CLI with --output json, return parsed output."""
    cmd = ["aws", *args, "--output", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True, env=_proxy_bypass_env())
    return json.loads(result.stdout)
