from datetime import UTC, datetime, timedelta

from ebs_janitor import delete_volumes, find_leaked

NOW = datetime(2026, 11, 3, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
AMI_NAMES = {"snap-ami": "al2023-ami-2023.10.20260302.1-kernel-6.1-x86_64"}


def vol(vid, tags=(), snap="snap-ami"):
    return {"VolumeId": vid, "Size": 150, "CreateTime": OLD, "SnapshotId": snap, "Tags": list(tags)}


def test_find_leaked_reads_tags_and_source_ami():
    volumes = [
        vol("vol-untagged"),
        vol("vol-runner", [{"Key": "Name", "Value": "gh-ci-action-runner"}]),
        vol("vol-other-name", [{"Key": "Name", "Value": "someones-scratch"}], snap="snap-other"),
        vol("vol-unknown-ami", snap="snap-gone"),
    ]
    looked_up = []

    def lookup(snapshot_ids):
        looked_up.extend(snapshot_ids)
        return AMI_NAMES

    leaked, _ = find_leaked(volumes, lookup, NOW)
    assert [v["VolumeId"] for v in leaked] == ["vol-untagged", "vol-runner"]
    assert "snap-other" not in looked_up  # rejected by the local checks before any AMI lookup


def test_per_run_cap_is_shared_across_regions():
    budget, deleted = [3], []

    def delete(*, VolumeId):  # keyword-only, like boto3
        deleted.append(VolumeId)

    delete_volumes("us-east-1", [vol(f"a{i}") for i in range(2)], delete, budget)
    delete_volumes("us-east-2", [vol(f"b{i}") for i in range(2)], delete, budget)
    assert deleted == ["a0", "a1", "b0"]


def test_repeated_errors_stop_the_region():
    calls = []

    def failing_delete(*, VolumeId):
        calls.append(VolumeId)
        raise RuntimeError("UnauthorizedOperation")

    assert not delete_volumes("us-east-1", [vol(f"v{i}") for i in range(20)], failing_delete, [100])
    assert len(calls) == 6


def test_any_delete_error_fails_the_run():
    def flaky_delete(*, VolumeId):
        if VolumeId == "v1":
            raise RuntimeError("VolumeInUse")

    assert not delete_volumes("us-east-1", [vol(f"v{i}") for i in range(3)], flaky_delete, [100])
