#!/usr/bin/env python3
"""Delete detached EBS root volumes left behind by legacy CI runners (see rules.py).

Dry run unless --execute.
"""

from __future__ import annotations

import argparse
import functools
import sys
from datetime import UTC, datetime

from rules import is_runner_ami, is_runner_volume

# The only account this deletes in (fbossci); refuse anything else.
ACCOUNT_ID = "308535385114"
# Regions the legacy runners launch in (pytorch-gha-infra var.aws_region_instances).
REGIONS = ["us-east-1", "us-east-2", "us-west-2"]
# Amazon Linux comes from amazon; Windows runner AMIs are built in the LF account
# (older ones in fbossci itself).
AMI_OWNERS = ["self", "amazon", "391835788720"]
MAX_ERRORS = 5


def find_leaked(volumes: list[dict], ami_lookup, now: datetime) -> tuple[list[dict], dict[str, str]]:
    """The DescribeVolumes entries that are leaked runner root disks, and their AMI names.

    AMI names are looked up only for volumes that pass the local checks.
    """
    candidates = [
        v for v in volumes if is_runner_volume({t["Key"]: t["Value"] for t in v.get("Tags", [])}, v["CreateTime"], now)
    ]
    ami_names = ami_lookup({v.get("SnapshotId", "") for v in candidates})
    return [v for v in candidates if is_runner_ami(ami_names.get(v.get("SnapshotId", "")))], ami_names


def delete_volumes(region: str, volumes: list[dict], delete, budget: list[int]) -> bool:
    """Delete volumes until the shared per-run budget runs out; False if any delete failed."""
    errors = 0
    for i, v in enumerate(volumes):
        if budget[0] <= 0:
            print(f"{region}: per-run cap reached, {len(volumes) - i} matching volumes left for the next run")
            return errors == 0
        budget[0] -= 1
        try:
            delete(VolumeId=v["VolumeId"])
            result = "deleted"
        except Exception as e:  # noqa: BLE001 - log, count and keep going
            errors += 1
            result = f"error: {e}"
        print(f"{region} {v['VolumeId']} {v['Size']}GiB -> {result}")
        if errors > MAX_ERRORS:
            print(f"{region}: stopping after {errors} errors")
            return False
    return errors == 0


def _ami_names(ec2, snapshot_ids: set[str]) -> dict[str, str]:
    """Source snapshot id -> AMI name, for AMIs still registered (deprecated ones included)."""
    names: dict[str, str] = {}
    ids = sorted(i for i in snapshot_ids if i)
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        images = ec2.describe_images(
            Owners=AMI_OWNERS,
            IncludeDeprecated=True,
            Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": chunk}],
        )["Images"]
        for image in images:
            for bdm in image.get("BlockDeviceMappings", []):
                if bdm.get("Ebs", {}).get("SnapshotId") in chunk:
                    names[bdm["Ebs"]["SnapshotId"]] = image["Name"]
    return names


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true", help="delete; without it this is a dry run")
    p.add_argument("--max-deletes", type=int, default=500, help="volume deletes per run, across all regions")
    args = p.parse_args()

    import boto3
    from botocore.config import Config

    account = boto3.client("sts").get_caller_identity()["Account"]
    if account != ACCOUNT_ID:
        print(f"refusing to run in account {account}, expected {ACCOUNT_ID}")
        return 2

    now = datetime.now(UTC)
    budget = [args.max_deletes]
    ok = True
    for region in REGIONS:
        ec2 = boto3.client("ec2", region_name=region, config=Config(retries={"mode": "adaptive", "max_attempts": 10}))
        pages = ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "status", "Values": ["available"]}])
        volumes = [v for page in pages for v in page["Volumes"]]
        leaked, ami_names = find_leaked(volumes, functools.partial(_ami_names, ec2), now)
        print(f"{region}: {len(leaked)} leaked runner root volumes, {sum(v['Size'] for v in leaked):,} GiB")
        if not args.execute:
            for v in leaked:
                print(f"{region} {v['VolumeId']} {v['Size']}GiB {ami_names[v['SnapshotId']]} -> would delete")
            continue
        ok = delete_volumes(region, leaked, ec2.delete_volume, budget) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
