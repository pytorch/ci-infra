"""Single source of truth for the runner-side node-fleet derivation and validation.

The fleet name flows from a runner def's optional ``node_fleet`` field (or the
instance family fallback) into the workflow pod's tolerations, node affinity, and
the listener's CAPACITY_AWARE_NODE_FLEET env var. Two consumers read this:
generate_runners.py and the load-test distribution.py.
"""

from __future__ import annotations

import re

# Reserved fleet names that workflow pods must never target.
# c7i-runner is the dedicated runner-pod control-plane pool; allowing a workflow
# pod to tolerate it would let user code preempt the orchestrator and break the
# priority-class ladder.
RESERVED_NODE_FLEET_NAMES = frozenset({"c7i-runner"})

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
