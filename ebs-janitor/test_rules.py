from datetime import UTC, datetime, timedelta

import pytest

from rules import (
    Action,
    Context,
    Policy,
    Snapshot,
    Volume,
    classify,
    is_expired_janitor_snapshot,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)
LINUX_SNAP, WINDOWS_SNAP, OTHER_SNAP = "snap-linux", "snap-windows", "snap-other"

POLICY = Policy(
    runner_names=["gh-ci-action-runner", "meta-pytorch-action-runner"],
    runner_ami_patterns=["al2023-ami-2023.*", "Windows 20?? GHA CI*"],
    gpu_dev_enabled=True,
)


def vol(tags=None, age_days=30, snap=LINUX_SNAP, vid="vol-1"):
    return Volume(vid, "us-east-1", 150, "gp3", NOW - timedelta(days=age_days), snap, tags or {})


def ctx(snapshots=None, live=("meta-prod-aws-ue1",)):
    return Context(
        now=NOW,
        ami_names={
            LINUX_SNAP: "al2023-ami-2023.10.20260302.1-kernel-6.1-x86_64",
            WINDOWS_SNAP: "Windows 2019 GHA CI - 20261002003950",
            OTHER_SNAP: "my-custom-ami",
        },
        snapshots_by_volume=snapshots or {},
        live_clusters=set(live),
    )


def snap(state="completed", age_days=1, tags=None, vid="vol-1"):
    return Snapshot("snap-x", vid, state, NOW - timedelta(days=age_days), tags or {})


def gpu_dev_tags(last_used_days_ago=10):
    return {
        "gpu-dev-user": "someone@example.com",
        "Project": "gpu-dev-servers",
        "disk_name": "default",
        "last_used": str((NOW - timedelta(days=last_used_days_ago)).timestamp()),
    }


# ---- guards ---------------------------------------------------------------


def test_keep_tag_wins_over_everything():
    assert classify(vol({"ebs-janitor/keep": ""}), ctx(), POLICY).action == Action.SKIP


def test_too_new_is_skipped():
    assert classify(vol(age_days=1), ctx(), POLICY).rule == "too-new"


# ---- runner root volumes ------------------------------------------------------


@pytest.mark.parametrize(
    "tags,snap_id",
    [
        ({}, LINUX_SNAP),
        ({"Name": "gh-ci-action-runner", "Project": "pytorch/pytorch"}, LINUX_SNAP),
        ({"Name": "meta-pytorch-action-runner"}, WINDOWS_SNAP),
    ],
)
def test_runner_root_is_deleted_without_snapshot(tags, snap_id):
    d = classify(vol(tags, snap=snap_id), ctx(), POLICY)
    assert (d.rule, d.action) == ("runner-root", Action.DELETE)


def test_unresolved_ami_is_not_a_runner_root():
    d = classify(vol({}, snap="snap-deregistered"), ctx(), POLICY)
    assert d.rule == "unknown"


def test_non_runner_ami_is_not_a_runner_root():
    d = classify(vol({"Name": "gh-ci-action-runner"}, snap=OTHER_SNAP), ctx(), POLICY)
    assert d.rule == "unknown"


def test_other_name_from_runner_ami_is_not_a_runner_root():
    d = classify(vol({"Name": "someones-scratch"}, snap=LINUX_SNAP), ctx(), POLICY)
    assert d.rule == "unknown"


# ---- gpu-dev --------------------------------------------------------------------


def test_gpu_dev_with_snapshot_after_last_use_is_deleted():
    s = snap(age_days=5, tags={"gpu-dev-user": "someone@example.com"})
    d = classify(vol(gpu_dev_tags(10), snap=""), ctx({"vol-1": [s]}), POLICY)
    assert (d.rule, d.action) == ("gpu-dev-backed-up", Action.DELETE)


@pytest.mark.parametrize(
    "s",
    [
        snap(age_days=20, tags={"gpu-dev-user": "u"}),  # taken before last use
        snap(state="pending", age_days=5, tags={"gpu-dev-user": "u"}),
        snap(age_days=5, tags={"gpu-dev-user": "u", "delete-date": "2026-10-01"}),  # soft-deleted
        snap(age_days=5, tags={}),  # not a gpu-dev snapshot (e.g. a security scanner's)
        snap(age_days=5, tags={"gpu-dev-user": "u"}, vid="vol-other"),  # another volume
    ],
)
def test_gpu_dev_without_durable_snapshot_gets_snapshotted(s):
    d = classify(vol(gpu_dev_tags(10), snap=""), ctx({s.volume_id: [s]}), POLICY)
    assert (d.rule, d.action) == ("gpu-dev-not-backed-up", Action.SNAPSHOT)


def test_gpu_dev_missing_last_used_gets_snapshotted():
    tags = gpu_dev_tags()
    del tags["last_used"]
    s = snap(age_days=1, tags={"gpu-dev-user": "u"})
    assert classify(vol(tags, snap=""), ctx({"vol-1": [s]}), POLICY).action == Action.SNAPSHOT


def test_gpu_dev_rule_disabled_reports_only():
    policy = Policy(runner_ami_patterns=POLICY.runner_ami_patterns, gpu_dev_enabled=False)
    assert classify(vol(gpu_dev_tags(), snap=""), ctx(), policy).action == Action.REPORT


# ---- janitor snapshots -------------------------------------------------------------


def test_completed_janitor_snapshot_means_delete():
    s = snap(tags={"ebs-janitor/managed": "true"})
    d = classify(vol({"Name": "x"}, snap=""), ctx({"vol-1": [s]}), POLICY)
    assert (d.rule, d.action) == ("janitor-snapshot-done", Action.DELETE)


def test_pending_janitor_snapshot_means_wait():
    s = snap(state="pending", tags={"ebs-janitor/managed": "true"})
    assert classify(vol({"Name": "x"}, snap=""), ctx({"vol-1": [s]}), POLICY).action == Action.WAIT


# ---- PVCs ----------------------------------------------------------------------------


def test_pvc_of_live_cluster_is_reported_only():
    tags = {"kubernetes.io/created-for/pvc/name": "data", "KubernetesCluster": "meta-prod-aws-ue1"}
    assert classify(vol(tags, snap=""), ctx(), POLICY).action == Action.REPORT


def test_pvc_of_deleted_cluster_found_via_cluster_tag_key():
    tags = {"ebs.csi.aws.com/cluster": "true", "kubernetes.io/cluster/pytorch-arc-staging": "owned"}
    d = classify(vol(tags, snap=""), ctx(), POLICY)
    assert (d.rule, d.action) == ("pvc-deleted-cluster", Action.SNAPSHOT)


def test_old_pvc_of_deleted_cluster_is_deleted_without_snapshot():
    tags = {"kubernetes.io/created-for/pvc/name": "data", "KubernetesCluster": "gone"}
    assert classify(vol(tags, age_days=400, snap=""), ctx(), POLICY).action == Action.DELETE


# ---- everything else ---------------------------------------------------------------------


def test_unknown_volume_is_tagged_first():
    assert classify(vol({"Name": "x"}, snap=""), ctx(), POLICY).action == Action.TAG_FIRST_SEEN


def test_unknown_volume_in_grace_is_skipped():
    tags = {"Name": "x", "ebs-janitor/first-seen": (NOW - timedelta(days=3)).date().isoformat()}
    assert classify(vol(tags, snap=""), ctx(), POLICY).rule == "unknown-in-grace"


def test_unknown_volume_past_grace_is_snapshotted():
    tags = {"Name": "x", "ebs-janitor/first-seen": (NOW - timedelta(days=15)).date().isoformat()}
    assert classify(vol(tags, snap=""), ctx(), POLICY).action == Action.SNAPSHOT


def test_old_unknown_volume_past_grace_is_deleted_without_snapshot():
    tags = {"Name": "x", "ebs-janitor/first-seen": (NOW - timedelta(days=15)).date().isoformat()}
    assert classify(vol(tags, age_days=800, snap=""), ctx(), POLICY).action == Action.DELETE


# ---- snapshot retention --------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags,expired",
    [
        ({"ebs-janitor/managed": "true", "ebs-janitor/delete-after": "2026-10-26"}, True),
        ({"ebs-janitor/managed": "true", "ebs-janitor/delete-after": "2026-10-27"}, False),
        ({"ebs-janitor/managed": "true", "ebs-janitor/delete-after": "never"}, False),
        ({"ebs-janitor/delete-after": "2020-01-01"}, False),  # not ours
    ],
)
def test_only_expired_janitor_snapshots_are_deleted(tags, expired):
    assert is_expired_janitor_snapshot(snap(tags=tags), NOW) is expired
