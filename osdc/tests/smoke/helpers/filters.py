"""Pure-Python filter utilities for batch-fetched Kubernetes resource lists."""

from __future__ import annotations

__all__ = [
    "filter_daemonsets",
    "filter_deployments",
    "filter_pods",
    "filter_services",
    "find_helm_release",
]


def filter_pods(all_pods: dict, namespace: str | None = None, labels: dict[str, str] | None = None) -> list[dict]:
    """Filter pods from batch-fetched pod list."""
    pods = all_pods.get("items", [])
    if namespace:
        pods = [p for p in pods if p["metadata"]["namespace"] == namespace]
    if labels:

        def _match(pod: dict) -> bool:
            pod_labels = pod.get("metadata", {}).get("labels", {})
            return all(pod_labels.get(k) == v for k, v in labels.items())

        pods = [p for p in pods if _match(p)]
    return pods


def filter_deployments(
    all_deployments: dict, namespace: str | None = None, name: str | None = None, name_contains: str | None = None
) -> list[dict]:
    """Filter deployments from batch-fetched list."""
    items = all_deployments.get("items", [])
    if namespace:
        items = [d for d in items if d["metadata"]["namespace"] == namespace]
    if name:
        items = [d for d in items if d["metadata"]["name"] == name]
    if name_contains:
        items = [d for d in items if name_contains in d["metadata"]["name"]]
    return items


def filter_daemonsets(
    all_daemonsets: dict, namespace: str | None = None, name: str | None = None, name_contains: str | None = None
) -> list[dict]:
    """Filter daemonsets from batch-fetched list."""
    items = all_daemonsets.get("items", [])
    if namespace:
        items = [d for d in items if d["metadata"]["namespace"] == namespace]
    if name:
        items = [d for d in items if d["metadata"]["name"] == name]
    if name_contains:
        items = [d for d in items if name_contains in d["metadata"]["name"]]
    return items


def filter_services(
    all_services: dict, namespace: str | None = None, name: str | None = None, name_contains: str | None = None
) -> list[dict]:
    """Filter services from batch-fetched list."""
    items = all_services.get("items", [])
    if namespace:
        items = [s for s in items if s["metadata"]["namespace"] == namespace]
    if name:
        items = [s for s in items if s["metadata"]["name"] == name]
    if name_contains:
        items = [s for s in items if name_contains in s["metadata"]["name"]]
    return items


def find_helm_release(all_releases: list[dict], name: str, namespace: str | None = None) -> dict | None:
    """Find a Helm release by name and optional namespace."""
    for rel in all_releases:
        if rel["name"] == name and (namespace is None or rel.get("namespace") == namespace):
            return rel
    return None
