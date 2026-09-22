#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "boto3>=1.34.0",
# ]
# ///
"""
Utility script to find and clear stale disk locks.

A stale lock occurs when a disk has in_use=True but the attached reservation
is no longer in an active state (expired, cancelled, failed).

Usage:
    # Dry run (default) - show what would be cleared
    AWS_PROFILE=fbossci ./clear_stale_disk_locks.py

    # Actually clear the stale locks
    AWS_PROFILE=fbossci ./clear_stale_disk_locks.py --fix

    # Use a different region
    AWS_PROFILE=fbossci ./clear_stale_disk_locks.py --region us-west-2
"""

import argparse
import boto3
from datetime import datetime, timezone


DISKS_TABLE = "pytorch-gpu-dev-disks"
RESERVATIONS_TABLE = "pytorch-gpu-dev-reservations"
ACTIVE_STATUSES = {"active", "preparing", "queued", "pending"}


def get_in_use_disks(dynamodb, region: str) -> list[dict]:
    """Scan disks table for entries with in_use=True."""
    table = dynamodb.Table(DISKS_TABLE)

    items = []
    response = table.scan(
        FilterExpression="in_use = :true",
        ExpressionAttributeValues={":true": True},
        ProjectionExpression="user_id, disk_name, in_use, attached_to_reservation"
    )
    items.extend(response.get("Items", []))

    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression="in_use = :true",
            ExpressionAttributeValues={":true": True},
            ProjectionExpression="user_id, disk_name, in_use, attached_to_reservation",
            ExclusiveStartKey=response["LastEvaluatedKey"]
        )
        items.extend(response.get("Items", []))

    return items


def get_reservation_statuses(dynamodb, reservation_ids: list[str]) -> dict[str, dict]:
    """Batch get reservation statuses."""
    if not reservation_ids:
        return {}

    table = dynamodb.Table(RESERVATIONS_TABLE)
    results = {}

    # Batch get in chunks of 100 (DynamoDB limit)
    for i in range(0, len(reservation_ids), 100):
        chunk = reservation_ids[i:i+100]
        response = dynamodb.batch_get_item(
            RequestItems={
                RESERVATIONS_TABLE: {
                    "Keys": [{"reservation_id": rid} for rid in chunk],
                    "ProjectionExpression": "reservation_id, #s, user_id, expires_at",
                    "ExpressionAttributeNames": {"#s": "status"}
                }
            }
        )

        for item in response.get("Responses", {}).get(RESERVATIONS_TABLE, []):
            results[item["reservation_id"]] = item

    return results


def clear_disk_lock(dynamodb, user_id: str, disk_name: str) -> bool:
    """Clear the in_use flag for a disk."""
    table = dynamodb.Table(DISKS_TABLE)

    try:
        table.update_item(
            Key={"user_id": user_id, "disk_name": disk_name},
            UpdateExpression="SET in_use = :false REMOVE attached_to_reservation",
            ExpressionAttributeValues={":false": False}
        )
        return True
    except Exception as e:
        print(f"  ❌ Failed to clear: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Find and clear stale disk locks")
    parser.add_argument("--fix", action="store_true", help="Actually clear stale locks (default is dry-run)")
    parser.add_argument("--region", default="us-east-2", help="AWS region (default: us-east-2)")
    args = parser.parse_args()

    print(f"🔍 Scanning for stale disk locks in {args.region}...")
    print(f"   Disks table: {DISKS_TABLE}")
    print(f"   Reservations table: {RESERVATIONS_TABLE}")
    print()

    session = boto3.Session(region_name=args.region)
    dynamodb = session.resource("dynamodb")

    # Get all disks marked as in_use
    in_use_disks = get_in_use_disks(dynamodb, args.region)
    print(f"📊 Found {len(in_use_disks)} disk(s) marked as in_use")

    if not in_use_disks:
        print("✅ No disks marked as in_use. Nothing to do.")
        return

    # Get reservation IDs and fetch their statuses
    reservation_ids = [
        d["attached_to_reservation"]
        for d in in_use_disks
        if d.get("attached_to_reservation")
    ]
    reservation_statuses = get_reservation_statuses(dynamodb, reservation_ids)

    # Find stale locks
    stale_locks = []
    valid_locks = []

    for disk in in_use_disks:
        user_id = disk["user_id"]
        disk_name = disk["disk_name"]
        res_id = disk.get("attached_to_reservation")

        if not res_id:
            # No reservation attached but marked in_use - definitely stale
            stale_locks.append({
                "user_id": user_id,
                "disk_name": disk_name,
                "reservation_id": None,
                "reason": "no reservation attached"
            })
            continue

        reservation = reservation_statuses.get(res_id)

        if not reservation:
            # Reservation doesn't exist anymore
            stale_locks.append({
                "user_id": user_id,
                "disk_name": disk_name,
                "reservation_id": res_id,
                "reason": "reservation not found"
            })
            continue

        status = reservation.get("status", "unknown")

        if status not in ACTIVE_STATUSES:
            stale_locks.append({
                "user_id": user_id,
                "disk_name": disk_name,
                "reservation_id": res_id,
                "status": status,
                "reason": f"reservation status is '{status}'"
            })
        else:
            valid_locks.append({
                "user_id": user_id,
                "disk_name": disk_name,
                "reservation_id": res_id,
                "status": status
            })

    # Report findings
    print()
    if valid_locks:
        print(f"✅ {len(valid_locks)} valid lock(s) (reservation is active):")
        for lock in valid_locks:
            print(f"   • {lock['user_id']} / {lock['disk_name']} → {lock['reservation_id'][:8]}... ({lock['status']})")

    print()
    if not stale_locks:
        print("✅ No stale locks found. All in_use disks have active reservations.")
        return

    print(f"⚠️  {len(stale_locks)} stale lock(s) found:")
    for lock in stale_locks:
        res_display = f"{lock['reservation_id'][:8]}..." if lock['reservation_id'] else "none"
        print(f"   • {lock['user_id']} / {lock['disk_name']} → {res_display} ({lock['reason']})")

    # Fix if requested
    print()
    if args.fix:
        print("🔧 Clearing stale locks...")
        cleared = 0
        for lock in stale_locks:
            print(f"   Clearing {lock['user_id']} / {lock['disk_name']}...", end=" ")
            if clear_disk_lock(dynamodb, lock["user_id"], lock["disk_name"]):
                print("✅")
                cleared += 1
        print()
        print(f"✅ Cleared {cleared}/{len(stale_locks)} stale lock(s)")
    else:
        print("ℹ️  Dry run mode. Use --fix to actually clear stale locks.")


if __name__ == "__main__":
    main()
