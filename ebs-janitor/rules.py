"""Decide whether a detached EBS volume is a leaked legacy CI runner root disk.

When a legacy EC2 runner is reused, its root volume is replaced, and the
replacement doesn't get DeleteOnTermination, so it outlives the instance
(pytorch/test-infra#7930). Both legacy fleets (pytorch and meta-pytorch, see
pytorch-gha-infra runners/regions/us-east-1/main.tf) boot from these AMIs.
"""

from __future__ import annotations

import fnmatch
from datetime import datetime, timedelta

RUNNER_NAMES = {"gh-ci-action-runner", "meta-pytorch-action-runner"}
RUNNER_AMI_PATTERNS = ("al2023-ami-2023.*", "Windows 20?? GHA CI*")
# Leaves a volume alone while its root-volume replacement may still be in progress.
MIN_AGE = timedelta(days=2)


def is_runner_volume(tags: dict[str, str], created: datetime, now: datetime) -> bool:
    """The local half of the rule: untagged (the scale-up Lambda tags only the
    instance) or named after a runner fleet, and at least MIN_AGE old."""
    if now - created < MIN_AGE:
        return False
    return not tags or tags.get("Name") in RUNNER_NAMES


def is_runner_ami(ami_name: str | None) -> bool:
    """The source AMI half of the rule. A source AMI that can't be resolved never matches."""
    return ami_name is not None and any(fnmatch.fnmatchcase(ami_name, p) for p in RUNNER_AMI_PATTERNS)


def is_leaked_runner_root(tags: dict[str, str], created: datetime, ami_name: str | None, now: datetime) -> bool:
    """True for a detached volume that is safe to delete."""
    return is_runner_volume(tags, created, now) and is_runner_ami(ami_name)
