from datetime import UTC, datetime, timedelta

from aws import snapshot_tags
from ebs_janitor import Budget, process_region, summarize
from rules import Policy, Snapshot, Volume

NOW = datetime(2026, 10, 27, tzinfo=UTC)
EMAIL = "someone@example.com"
POLICY = Policy(
    runner_names=["gh-ci-action-runner"],
    runner_ami_patterns=["al2023-ami-2023.*"],
    gpu_dev_enabled=True,
)


def runner_vol(vid):
    return Volume(vid, "us-east-1", 150, "gp3", NOW - timedelta(days=30), "snap-ami", {})


def gpu_dev_vol(vid):
    tags = {"gpu-dev-user": EMAIL, "disk_name": "default", "last_used": str((NOW - timedelta(days=9)).timestamp())}
    return Volume(vid, "us-east-1", 1024, "gp3", NOW - timedelta(days=30), "", tags, 16000, 1000)


class FakeClient:
    region = "us-east-1"

    def __init__(self, volumes, denied=(), fail=(), gone=(), janitor_snaps=()):
        self.volumes = volumes
        self.denied = list(denied)
        self.fail = set(fail)
        self.gone = set(gone)
        self.janitor_snaps = list(janitor_snaps)
        self.calls = []

    def detached_volumes(self):
        return list(self.volumes)

    def ami_names(self, snapshot_ids, owners):
        return {"snap-ami": "al2023-ami-2023.10-x86_64"}

    def snapshots_by_volume(self, ids):
        return {}

    def live_clusters(self):
        return set()

    def janitor_snapshots(self):
        return self.janitor_snaps

    def preflight(self, volume_id):
        self.calls.append(("preflight", volume_id))
        return self.denied

    def recheck(self, volumes):
        return {v.id: v for v in volumes if v.id not in self.gone}

    def delete_volume(self, vid):
        self.calls.append(("delete", vid))
        if vid in self.fail:
            raise RuntimeError("boom")

    def snapshot(self, v, rule, now, retention_days):
        self.calls.append(("snapshot", v.id))
        return "snap-new"

    def tag(self, rid, tags):
        self.calls.append(("tag", rid))

    def delete_snapshot(self, s):
        self.calls.append(("delete-snapshot", s.id))


def budget(n=100):
    return Budget(n, n, n)


def mutations(client):
    return [c for c in client.calls if c[0] != "preflight"]


def test_dry_run_makes_no_changes():
    c = FakeClient([runner_vol("vol-a"), gpu_dev_vol("vol-b")])
    r = process_region(c, POLICY, NOW, execute=False, budget=budget())
    assert c.calls == []
    assert {row.result for row in r.rows} == {"planned"}


def test_execute_deletes_and_snapshots():
    c = FakeClient([runner_vol("vol-a"), gpu_dev_vol("vol-b")])
    process_region(c, POLICY, NOW, execute=True, budget=budget())
    assert mutations(c) == [("delete", "vol-a"), ("snapshot", "vol-b")]


def test_failed_preflight_aborts_before_any_change():
    c = FakeClient([runner_vol("vol-a")], denied=["ec2:DeleteVolume (UnauthorizedOperation)"])
    r = process_region(c, POLICY, NOW, execute=True, budget=budget())
    assert mutations(c) == [] and r.aborted


def test_volume_reattached_since_scan_is_skipped():
    c = FakeClient([runner_vol("vol-a")], gone={"vol-a"})
    r = process_region(c, POLICY, NOW, execute=True, budget=budget())
    assert mutations(c) == [] and r.rows[0].result == "skipped: no longer detached"


def test_per_run_cap_is_shared_and_respected():
    b = budget(2)
    c = FakeClient([runner_vol(f"vol-{i}") for i in range(5)])
    process_region(c, POLICY, NOW, execute=True, budget=b)
    assert len(mutations(c)) == 2 and b.deletes == 0


def test_repeated_errors_abort_the_region():
    vols = [runner_vol(f"vol-{i}") for i in range(50)]
    c = FakeClient(vols, fail={v.id for v in vols})
    r = process_region(c, POLICY, NOW, execute=True, budget=budget())
    assert r.aborted and len(mutations(c)) == 6


def test_expired_janitor_snapshots_are_deleted():
    s = Snapshot(
        "snap-old",
        "vol-z",
        "completed",
        NOW - timedelta(days=100),
        {"ebs-janitor/managed": "true", "ebs-janitor/delete-after": "2026-10-01"},
    )
    c = FakeClient([], janitor_snaps=[s])
    process_region(c, POLICY, NOW, execute=True, budget=budget())
    assert mutations(c) == [("delete-snapshot", "snap-old")]


def test_reports_never_contain_tag_values():
    c = FakeClient([gpu_dev_vol("vol-b")])
    r = process_region(c, POLICY, NOW, execute=False, budget=budget())
    text = repr(r.rows) + summarize([r], execute=False)
    assert EMAIL not in text


def test_janitor_snapshot_is_invisible_to_gpu_dev_restore():
    # gpu-dev restores the newest snapshot tagged gpu-dev-user (+ disk_name) and
    # deletes ones tagged delete-date; a janitor backup must carry neither.
    tags = {t["Key"]: t["Value"] for t in snapshot_tags(gpu_dev_vol("vol-b"), "gpu-dev-not-backed-up", NOW, 90)}
    assert not {"gpu-dev-user", "disk_name", "delete-date"} & tags.keys()
    assert tags["ebs-janitor/orig/gpu-dev-user"] == EMAIL
    assert tags["ebs-janitor/managed"] == "true"
    assert tags["ebs-janitor/delete-after"] == "2027-01-25"
