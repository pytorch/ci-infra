"""Decide what to do with a detached EBS volume.

Pure functions only: no AWS calls, so every rule can be unit tested. The
caller gathers the facts (volume, snapshots, AMI names, live clusters) and
applies the returned action.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

MANAGED_TAG = "ebs-janitor/managed"
SOURCE_VOLUME_TAG = "ebs-janitor/source-volume"
FIRST_SEEN_TAG = "ebs-janitor/first-seen"
DELETE_AFTER_TAG = "ebs-janitor/delete-after"

GPU_DEV_USER_TAG = "gpu-dev-user"
GPU_DEV_LAST_USED_TAG = "last_used"
# gpu-dev's own sweeper deletes snapshots carrying this tag, so a snapshot with
# it is not a durable backup.
GPU_DEV_SOFT_DELETE_TAG = "delete-date"

PVC_TAGS = ("kubernetes.io/created-for/pvc/name", "ebs.csi.aws.com/cluster", "CSIVolumeName")
CLUSTER_NAME_TAGS = ("KubernetesCluster", "ebs.csi.aws.com/cluster-name", "eks:eks-cluster-name")


class Action(str, Enum):
    SKIP = "skip"
    WAIT = "wait"  # a janitor snapshot is still pending
    REPORT = "report"  # leave alone, list in the report
    TAG_FIRST_SEEN = "tag-first-seen"
    SNAPSHOT = "snapshot"  # create a janitor snapshot, delete on a later run
    DELETE = "delete"


@dataclass
class Policy:
    keep_tag: str = "ebs-janitor/keep"
    min_age_days: int = 2
    grace_days: int = 14
    old_volume_days: int = 365
    snapshot_retention_days: int = 90
    runner_names: list[str] = field(default_factory=list)
    runner_ami_patterns: list[str] = field(default_factory=list)
    runner_ami_owners: list[str] = field(default_factory=lambda: ["self", "amazon"])
    gpu_dev_enabled: bool = False
    gpu_dev_snapshot_slack_seconds: int = 300
    regions: list[str] = field(default_factory=list)
    exclude_regions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> Policy:
        runner = d.get("runner_root", {})
        gpu_dev = d.get("gpu_dev", {})
        return cls(
            keep_tag=d.get("keep_tag", cls.keep_tag),
            min_age_days=d.get("min_age_days", cls.min_age_days),
            grace_days=d.get("grace_days", cls.grace_days),
            old_volume_days=d.get("old_volume_days", cls.old_volume_days),
            snapshot_retention_days=d.get("snapshot_retention_days", cls.snapshot_retention_days),
            runner_names=runner.get("names", []),
            runner_ami_patterns=runner.get("ami_name_patterns", []),
            runner_ami_owners=runner.get("ami_owners", ["self", "amazon"]),
            gpu_dev_enabled=gpu_dev.get("enabled", False),
            gpu_dev_snapshot_slack_seconds=gpu_dev.get("snapshot_slack_seconds", 300),
            regions=d.get("regions", []),
            exclude_regions=d.get("exclude_regions", []),
        )


@dataclass
class Volume:
    id: str
    region: str
    size: int
    type: str
    created: datetime
    snapshot_id: str
    tags: dict[str, str]
    iops: int | None = None
    throughput: int | None = None


@dataclass
class Snapshot:
    id: str
    volume_id: str
    state: str
    start_time: datetime
    tags: dict[str, str]


@dataclass
class Context:
    now: datetime
    # source snapshot id -> name of the AMI it backs; missing means unresolved
    ami_names: dict[str, str]
    # self-owned snapshots taken from each volume
    snapshots_by_volume: dict[str, list[Snapshot]]
    live_clusters: set[str]


@dataclass
class Decision:
    rule: str
    action: Action
    detail: str = ""


def _age_days(ctx: Context, when: datetime) -> float:
    return (ctx.now - when).total_seconds() / 86400


def _cluster_of(v: Volume) -> str | None:
    for key in CLUSTER_NAME_TAGS:
        if v.tags.get(key):
            return v.tags[key]
    for key in v.tags:
        if key.startswith("kubernetes.io/cluster/"):
            return key.removeprefix("kubernetes.io/cluster/")
    return None


def _is_pvc(v: Volume) -> bool:
    return any(k in v.tags for k in PVC_TAGS)


def _is_gpu_dev(v: Volume) -> bool:
    return GPU_DEV_USER_TAG in v.tags or v.tags.get("Project") == "gpu-dev-servers"


def is_runner_root(v: Volume, ctx: Context, policy: Policy) -> bool:
    if _is_pvc(v) or _is_gpu_dev(v):
        return False
    name = v.tags.get("Name")
    if name is not None and name not in policy.runner_names:
        return False
    ami = ctx.ami_names.get(v.snapshot_id)
    if ami is None:  # unresolved source: not provably a runner disk
        return False
    return any(fnmatch.fnmatchcase(ami, p) for p in policy.runner_ami_patterns)


def _janitor_snapshot(v: Volume, ctx: Context) -> Snapshot | None:
    """Newest janitor snapshot of this volume, completed preferred."""
    ours = [s for s in ctx.snapshots_by_volume.get(v.id, []) if s.tags.get(MANAGED_TAG) == "true"]
    if not ours:
        return None
    completed = [s for s in ours if s.state == "completed"]
    pool = completed or ours
    return max(pool, key=lambda s: s.start_time)


def has_durable_gpu_dev_snapshot(v: Volume, ctx: Context, policy: Policy) -> bool:
    """A completed gpu-dev snapshot of this exact volume, taken after its last use."""
    try:
        last_used = float(v.tags[GPU_DEV_LAST_USED_TAG])
    except (KeyError, ValueError):
        return False
    for s in ctx.snapshots_by_volume.get(v.id, []):
        if s.state != "completed":
            continue
        if GPU_DEV_USER_TAG not in s.tags or GPU_DEV_SOFT_DELETE_TAG in s.tags:
            continue
        if s.start_time.timestamp() >= last_used - policy.gpu_dev_snapshot_slack_seconds:
            return True
    return False


def _snapshot_or_delete_old(v: Volume, ctx: Context, policy: Policy, rule: str) -> Decision:
    if _age_days(ctx, v.created) > policy.old_volume_days:
        return Decision(rule, Action.DELETE, f"created over {policy.old_volume_days} days ago")
    return Decision(rule, Action.SNAPSHOT)


def classify(v: Volume, ctx: Context, policy: Policy) -> Decision:
    """First matching rule wins. See README.md for the rule table."""
    if policy.keep_tag in v.tags:
        return Decision("keep-tag", Action.SKIP)

    if _age_days(ctx, v.created) < policy.min_age_days:
        return Decision("too-new", Action.SKIP)

    snap = _janitor_snapshot(v, ctx)
    if snap is not None:
        if snap.state == "completed":
            return Decision("janitor-snapshot-done", Action.DELETE, snap.id)
        return Decision("janitor-snapshot-pending", Action.WAIT, snap.id)

    if is_runner_root(v, ctx, policy):
        return Decision("runner-root", Action.DELETE, ctx.ami_names[v.snapshot_id])

    if _is_gpu_dev(v):
        if not policy.gpu_dev_enabled:
            return Decision("gpu-dev-disabled", Action.REPORT)
        if has_durable_gpu_dev_snapshot(v, ctx, policy):
            return Decision("gpu-dev-backed-up", Action.DELETE)
        return Decision("gpu-dev-not-backed-up", Action.SNAPSHOT)

    if _is_pvc(v):
        cluster = _cluster_of(v)
        if cluster in ctx.live_clusters:
            return Decision("pvc-live-cluster", Action.REPORT, cluster or "")
        return _snapshot_or_delete_old(v, ctx, policy, "pvc-deleted-cluster")

    first_seen = v.tags.get(FIRST_SEEN_TAG)
    if first_seen is None:
        return Decision("unknown", Action.TAG_FIRST_SEEN)
    try:
        seen = datetime.fromisoformat(first_seen).replace(tzinfo=ctx.now.tzinfo)
    except ValueError:
        return Decision("unknown", Action.TAG_FIRST_SEEN, "unparseable first-seen tag")
    if ctx.now - seen < timedelta(days=policy.grace_days):
        return Decision("unknown-in-grace", Action.SKIP, first_seen)
    return _snapshot_or_delete_old(v, ctx, policy, "unknown")


def is_expired_janitor_snapshot(s: Snapshot, now: datetime) -> bool:
    """Only snapshots this tool created, and only once their delete-after date has passed."""
    if s.tags.get(MANAGED_TAG) != "true":
        return False
    delete_after = s.tags.get(DELETE_AFTER_TAG)
    if not delete_after:
        return False
    try:
        return now.date() > datetime.fromisoformat(delete_after).date()
    except ValueError:
        return False


LIST_PRICE_GB_MONTH = {
    "gp3": 0.08,
    "gp2": 0.10,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.015,
    "standard": 0.05,
}


def list_price_month(v: Volume) -> float:
    """us-east-1 list price per month; good enough to rank and total, not to invoice."""
    cost = v.size * LIST_PRICE_GB_MONTH.get(v.type, 0.08)
    if v.type == "gp3":
        cost += max((v.iops or 3000) - 3000, 0) * 0.005
        cost += max((v.throughput or 125) - 125, 0) * 0.04
    return cost
