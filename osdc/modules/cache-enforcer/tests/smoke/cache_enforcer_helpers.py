"""Helper functions for cache-enforcer smoke tests."""

from __future__ import annotations

import re

from helpers import get_unstable_node_names


def get_init_failures(pods: list[dict], nodes: dict) -> list[str]:
    """Check init container status on stable nodes, returning failure descriptions.

    Skips pods on unstable nodes and transient waiting states
    (PodInitializing, ContainerCreating).
    """
    unstable_names = get_unstable_node_names(nodes)
    stable_pods = [p for p in pods if p["spec"].get("nodeName") not in unstable_names]
    failures: list[str] = []
    for pod in stable_pods:
        pod_name = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("initContainerStatuses", []):
            terminated = cs.get("state", {}).get("terminated")
            waiting = cs.get("state", {}).get("waiting")
            if waiting:
                reason = waiting.get("reason", "Unknown")
                # PodInitializing and ContainerCreating are transient — not failures
                if reason not in ("PodInitializing", "ContainerCreating"):
                    failures.append(f"{pod_name}/{cs['name']}: waiting ({reason})")
            elif terminated and terminated.get("exitCode", -1) != 0:
                reason = terminated.get("reason", "Unknown")
                failures.append(f"{pod_name}/{cs['name']}: exit {terminated['exitCode']} ({reason})")
    return failures


def domain_in_variable_block(script: str, var_name: str, domain: str) -> bool:
    """Check if a domain appears in a shell variable assignment block.

    Looks for the pattern:
        VAR_NAME="
        domain1
        domain2
        "
    and checks if `domain` is one of the listed values.
    """
    # Match: VAR_NAME="<content>"  (with embedded newlines)
    pattern = rf'{var_name}="(.*?)"'
    match = re.search(pattern, script, re.DOTALL)
    if not match:
        return False
    block = match.group(1)
    # Split on whitespace and check if domain is listed
    domains = block.split()
    return domain in domains
