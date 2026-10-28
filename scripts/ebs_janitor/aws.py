"""Thin boto3 layer: gather facts for rules.py and apply its actions."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from rules import (
    DELETE_AFTER_TAG,
    MANAGED_TAG,
    SOURCE_VOLUME_TAG,
    Snapshot,
    Volume,
)

BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})
MAX_TAGS = 50
ORIG_TAG_PREFIX = "ebs-janitor/orig/"
# Tag keys that make gpu-dev treat a snapshot as a restore source or as
# soft-deleted. Copies of them are always stored under ORIG_TAG_PREFIX, never
# under their own key.
_DANGEROUS_KEYS = {"gpu-dev-user", "disk_name", "delete-date"}


def enabled_regions() -> list[str]:
    ec2 = boto3.client("ec2", region_name="us-east-1", config=BOTO_CONFIG)
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


class RegionClient:
    """All AWS calls for one region. Mutating calls are rate limited."""

    def __init__(self, region: str, min_interval: float = 0.2) -> None:
        self.region = region
        self.ec2 = boto3.client("ec2", region_name=region, config=BOTO_CONFIG)
        self.eks = boto3.client("eks", region_name=region, config=BOTO_CONFIG)
        self._min_interval = min_interval
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self._last_call + self._min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # ---- facts -------------------------------------------------------------

    def detached_volumes(self) -> list[Volume]:
        out = []
        pages = self.ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "status", "Values": ["available"]}]
        )
        for page in pages:
            out.extend(_volume(self.region, v) for v in page["Volumes"])
        return out

    def recheck(self, volumes: list[Volume]) -> dict[str, Volume]:
        """Current state of the given volumes that are still detached."""
        current: dict[str, Volume] = {}
        ids = [v.id for v in volumes]
        for i in range(0, len(ids), 200):
            pages = self.ec2.get_paginator("describe_volumes").paginate(
                Filters=[
                    {"Name": "volume-id", "Values": ids[i : i + 200]},
                    {"Name": "status", "Values": ["available"]},
                ]
            )
            for page in pages:
                for raw in page["Volumes"]:
                    current[raw["VolumeId"]] = _volume(self.region, raw)
        return current

    def snapshots_by_volume(self, volume_ids: set[str]) -> dict[str, list[Snapshot]]:
        out: dict[str, list[Snapshot]] = {}
        for page in self.ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
            for s in page["Snapshots"]:
                if s.get("VolumeId") in volume_ids:
                    out.setdefault(s["VolumeId"], []).append(_snapshot(s))
        return out

    def janitor_snapshots(self) -> list[Snapshot]:
        pages = self.ec2.get_paginator("describe_snapshots").paginate(
            OwnerIds=["self"], Filters=[{"Name": f"tag:{MANAGED_TAG}", "Values": ["true"]}]
        )
        return [_snapshot(s) for page in pages for s in page["Snapshots"]]

    def ami_names(self, snapshot_ids: set[str], owners: list[str]) -> dict[str, str]:
        """Source snapshot id -> AMI name, for AMIs still registered."""
        out: dict[str, str] = {}
        ids = sorted(i for i in snapshot_ids if i)
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            images = self.ec2.describe_images(
                Owners=owners,
                IncludeDeprecated=True,
                Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": chunk}],
            )["Images"]
            for image in images:
                for bdm in image.get("BlockDeviceMappings", []):
                    sid = bdm.get("Ebs", {}).get("SnapshotId")
                    if sid in chunk:
                        out[sid] = image.get("Name", "")
        return out

    def live_clusters(self) -> set[str]:
        clusters: set[str] = set()
        for page in self.eks.get_paginator("list_clusters").paginate():
            clusters.update(page["clusters"])
        return clusters

    # ---- actions -----------------------------------------------------------

    def preflight(self, volume_id: str) -> list[str]:
        """Return the actions this role may NOT perform, checked with DryRun."""
        denied = []
        checks = {
            "ec2:DeleteVolume": lambda: self.ec2.delete_volume(VolumeId=volume_id, DryRun=True),
            "ec2:CreateSnapshot": lambda: self.ec2.create_snapshot(VolumeId=volume_id, DryRun=True),
            "ec2:CreateTags": lambda: self.ec2.create_tags(
                Resources=[volume_id], Tags=[{"Key": "ebs-janitor/preflight", "Value": "1"}], DryRun=True
            ),
        }
        for name, call in checks.items():
            try:
                call()
            except ClientError as e:
                if e.response["Error"]["Code"] != "DryRunOperation":
                    denied.append(f"{name} ({e.response['Error']['Code']})")
        return denied

    def tag(self, resource_id: str, tags: dict[str, str]) -> None:
        self._throttle()
        self.ec2.create_tags(Resources=[resource_id], Tags=[{"Key": k, "Value": v} for k, v in tags.items()])

    def delete_volume(self, volume_id: str) -> None:
        self._throttle()
        self.ec2.delete_volume(VolumeId=volume_id)

    def snapshot(self, v: Volume, rule: str, now: datetime, retention_days: int) -> str:
        self._throttle()
        resp = self.ec2.create_snapshot(
            VolumeId=v.id,
            Description=f"ebs-janitor backup of detached volume {v.id} ({rule})",
            TagSpecifications=[{"ResourceType": "snapshot", "Tags": snapshot_tags(v, rule, now, retention_days)}],
        )
        return resp["SnapshotId"]

    def delete_snapshot(self, s: Snapshot) -> None:
        if s.tags.get(MANAGED_TAG) != "true":  # never touch snapshots we didn't create
            raise ValueError(f"refusing to delete unmanaged snapshot {s.id}")
        self._throttle()
        self.ec2.delete_snapshot(SnapshotId=s.id)


def snapshot_tags(v: Volume, rule: str, now: datetime, retention_days: int) -> list[dict[str, str]]:
    delete_after = "never" if retention_days <= 0 else (now + timedelta(days=retention_days)).date().isoformat()
    tags = {
        "Name": f"ebs-janitor-{v.id}",
        MANAGED_TAG: "true",
        SOURCE_VOLUME_TAG: v.id,
        "ebs-janitor/rule": rule,
        DELETE_AFTER_TAG: delete_after,
    }
    for key, value in sorted(v.tags.items()):
        if len(tags) >= MAX_TAGS:
            break
        if key.startswith(("aws:", "ebs-janitor/")):
            continue
        tags[(ORIG_TAG_PREFIX + key)[:128]] = value[:256]
    assert not _DANGEROUS_KEYS & tags.keys()
    return [{"Key": k, "Value": val} for k, val in tags.items()]


def _volume(region: str, raw: dict) -> Volume:
    return Volume(
        id=raw["VolumeId"],
        region=region,
        size=raw["Size"],
        type=raw["VolumeType"],
        created=raw["CreateTime"],
        snapshot_id=raw.get("SnapshotId") or "",
        tags={t["Key"]: t["Value"] for t in raw.get("Tags", [])},
        iops=raw.get("Iops"),
        throughput=raw.get("Throughput"),
    )


def _snapshot(raw: dict) -> Snapshot:
    return Snapshot(
        id=raw["SnapshotId"],
        volume_id=raw.get("VolumeId", ""),
        state=raw["State"],
        start_time=raw["StartTime"],
        tags={t["Key"]: t["Value"] for t in raw.get("Tags", [])},
    )
