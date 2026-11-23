"""Single source of truth for the runner-side node-fleet derivation and validation.

The fleet name flows from a runner def's optional ``node_fleet`` field (or the
instance family fallback) into the workflow pod's tolerations, node affinity, and
the listener's CAPACITY_AWARE_NODE_FLEET env var. Two consumers read this:
generate_runners.py and the load-test distribution.py.
"""

from __future__ import annotations

import re

# Reserved fleet names that workflow pods must never target.
# c7i-runner (x86) and arm64-runner (Graviton) are the dedicated runner-pod
# control-plane pools; allowing a workflow pod to tolerate one would let user code
# preempt the orchestrator and break the priority-class ladder.
RUNNER_POD_NODE_FLEETS = {"amd64": "c7i-runner", "arm64": "arm64-runner"}
RESERVED_NODE_FLEET_NAMES = frozenset(RUNNER_POD_NODE_FLEETS.values())

# K8s DNS-1123 label: lowercase alphanumeric and dashes, start/end alphanumeric,
# 1-63 chars. Matches the constraints applied at apply time by the API server.
_NODE_FLEET_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def validate_node_fleet(value):
    """Validate a node_fleet override value.

    Returns ``(True, None)`` if valid, else ``(False, error_message)``. The error
    message describes the violation in operator-friendly terms.
    """
    if not isinstance(value, str):
        return False, f"must be a string, got {type(value).__name__}"
    if not _NODE_FLEET_PATTERN.match(value):
        return (
            False,
            "must be a DNS-1123 label (lowercase alphanumeric and dashes, "
            "1-63 chars, must start and end with alphanumeric)",
        )
    if value in RESERVED_NODE_FLEET_NAMES:
        return False, f"{value!r} is reserved and cannot be used as an override"
    return True, None


def derive_release_runner_group(cluster_runner_group):
    """GitHub runner group a release-class runner registers in.

    Release wheel builds are kept in a per-region group separate from CI. Given
    the cluster's base group, append the ``-release-runners`` suffix; a group-less
    cluster falls back to ``default``. Both the runner generator and the
    integration-test harness derive the group through here so they can't drift.
    """
    return f"{cluster_runner_group}-release-runners" if cluster_runner_group else "default"


def derive_fleet_name(instance_type, override=None):
    """Derive the node-fleet name from an instance type, or honor an explicit override.

    When ``override`` is not None, it is validated via :func:`validate_node_fleet`
    and returned verbatim if valid; a ``ValueError`` is raised otherwise. When
    ``override`` is None, the instance family (everything before the first dot)
    is returned.
    """
    if override is not None:
        ok, err = validate_node_fleet(override)
        if not ok:
            raise ValueError(f"node_fleet override invalid ({err}): got {override!r}")
        return override
    return instance_type.split(".")[0]


def derive_runner_pod_node_fleet(arch):
    """Runner-pod pool for a runner whose workflow pods run on ``arch``.

    The runner pod hosts the runner agent, and GitHub reports the agent's
    architecture as ``runner.arch`` / ``RUNNER_ARCH``. Putting it on a pool of
    the job's architecture keeps those values true for the job. Raises
    ``ValueError`` for an architecture with no runner-pod pool.
    """
    try:
        return RUNNER_POD_NODE_FLEETS[arch]
    except KeyError:
        raise ValueError(
            f"no runner-pod pool for arch {arch!r}; expected one of {sorted(RUNNER_POD_NODE_FLEETS)}"
        ) from None
