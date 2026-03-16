#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Identify stale ARC runner Helm releases and their orphaned history secrets.

Used by deploy.sh Steps 4 and 5 to clean up runners that were removed from defs/.

This module provides pure-logic functions (no subprocess calls) that determine:
  - Which deployed ConfigMaps are stale (not in the expected set)
  - Which Helm release names correspond to those stale runners
  - Which Helm history secrets are orphaned (belong to stale releases)
"""

from __future__ import annotations


def normalize_name(name: str) -> str:
    """Normalize runner name for K8s resources (dots/underscores → dashes)."""
    return name.replace(".", "-").replace("_", "-")


def expected_runner_names(generated_filenames: list[str]) -> list[str]:
    """Derive normalized runner names from generated YAML filenames.

    Args:
        generated_filenames: Basenames of generated files (e.g. ["a.linux.cpu.yaml"]).

    Returns:
        Normalized names (e.g. ["a-linux-cpu"]).
    """
    names = []
    for fname in generated_filenames:
        raw = fname.removesuffix(".yaml")
        names.append(normalize_name(raw))
    return names


def find_stale_runners(
    expected_names: list[str],
    deployed_configmap_names: list[str],
) -> list[str]:
    """Identify deployed ConfigMaps that are not in the expected set.

    Args:
        expected_names: Normalized runner names from current defs.
        deployed_configmap_names: ConfigMap names from the cluster
            (format: "arc-runner-hook-<normalized_name>").

    Returns:
        List of normalized runner names that are stale (deployed but not expected).
    """
    expected_set = set(expected_names)
    stale = []
    for cm_name in deployed_configmap_names:
        # Strip the "arc-runner-hook-" prefix to get the normalized runner name
        prefix = "arc-runner-hook-"
        if not cm_name.startswith(prefix):
            continue
        local_name = cm_name[len(prefix) :]
        if local_name not in expected_set:
            stale.append(local_name)
    return stale


def stale_release_names(stale_runner_names: list[str]) -> list[str]:
    """Convert stale runner names to Helm release names.

    Args:
        stale_runner_names: Normalized runner names (e.g. ["old-runner"]).

    Returns:
        Helm release names (e.g. ["arc-old-runner"]).
    """
    return [f"arc-{name}" for name in stale_runner_names]


def find_orphaned_secrets(
    stale_releases: list[str],
    helm_secrets: list[dict[str, str]],
) -> list[str]:
    """Identify Helm history secrets that belong to stale releases.

    Args:
        stale_releases: Helm release names that were uninstalled
            (e.g. ["arc-old-runner"]).
        helm_secrets: List of dicts with "secret_name" and "release_name" keys,
            representing all Helm-owned secrets in the namespace.

    Returns:
        Secret names that should be deleted (orphaned history from stale releases).
    """
    stale_set = set(stale_releases)
    orphans = []
    for secret in helm_secrets:
        if secret.get("release_name") in stale_set:
            orphans.append(secret["secret_name"])
    return orphans
