#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Wheel syncer daemon: downloads built wheels from S3 to local EFS wheelhouse.

Runs as a long-lived pod. Each cycle:
1. For each configured CUDA slug, lists wheels in S3 under {slug}/
2. Compares against local wheelhouse directory on EFS
3. Downloads missing wheels (atomic rename for safe placement)
4. Sleeps and repeats
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import time
from pathlib import Path


def list_wheels(bucket: str, prefix: str, s3_client) -> list[dict]:
    """List all .whl objects under a prefix in S3 with pagination.

    Returns list of {"key": str, "size": int} for objects ending in .whl.
    """
    wheels: list[dict] = []
    kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3_client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".whl"):
                wheels.append({"key": key, "size": obj["Size"]})
        if not resp.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]
    return wheels


def sync_slug(bucket: str, slug: str, wheelhouse_dir: Path, s3_client) -> tuple[int, int]:
    """Sync wheels for one CUDA slug from S3 to local wheelhouse.

    Returns (downloaded_count, skipped_count).
    Raises on S3 list failures. Individual download failures are logged and skipped.
    """
    prefix = f"{slug}/"
    wheels = list_wheels(bucket, prefix, s3_client)

    slug_dir = wheelhouse_dir / slug
    os.makedirs(slug_dir, exist_ok=True)

    downloaded = 0
    skipped = 0

    for wheel in wheels:
        key = wheel["key"]
        filename = key[len(prefix) :]
        if not filename:
            continue

        final_path = (slug_dir / filename).resolve()
        if not str(final_path).startswith(str(slug_dir.resolve()) + os.sep):
            print(f"WARNING: Skipping suspicious key {key} (path traversal)")
            continue
        if final_path.exists():
            skipped += 1
            continue

        tmp_path = final_path.parent / f"{final_path.name}.tmp"
        try:
            s3_client.download_file(bucket, key, str(tmp_path))
            os.rename(tmp_path, final_path)
            downloaded += 1
        except Exception as e:
            print(f"WARNING: Failed to download {key}: {e}")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)

    return downloaded, skipped


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Sync built wheels from S3 to local EFS wheelhouse")
    parser.add_argument("--wheelhouse-dir", required=True, help="EFS wheelhouse root directory")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--slugs", required=True, help="Comma-separated CUDA slugs (e.g. cpu,cu121,cu124)")
    parser.add_argument("--interval", type=int, default=60, help="Sleep interval between sync cycles in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single iteration and exit")
    return parser.parse_args(argv)


def run(args: argparse.Namespace, s3_client) -> None:
    """Main loop: sync wheels from S3 to local wheelhouse for each slug."""
    wheelhouse_dir = Path(args.wheelhouse_dir)
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()]

    shutdown = False

    def handle_sigterm(_signum, _frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGTERM, handle_sigterm)

    print(f"Wheel syncer starting: bucket={args.bucket}, slugs={slugs}")

    while not shutdown:
        total_downloaded = 0
        total_skipped = 0

        for slug in slugs:
            try:
                downloaded, skipped = sync_slug(args.bucket, slug, wheelhouse_dir, s3_client)
                total_downloaded += downloaded
                total_skipped += skipped
                if downloaded > 0:
                    print(f"[{slug}] Downloaded {downloaded} wheels, skipped {skipped}")
            except Exception as e:
                print(f"WARNING: Failed to sync slug {slug}: {e}")

        if total_downloaded > 0 or total_skipped == 0:
            print(f"Sync complete: {total_downloaded} downloaded, {total_skipped} skipped")

        Path("/tmp/last-success").touch()

        if args.once:
            return
        time.sleep(args.interval)


def main() -> None:
    """Entry point."""
    args = parse_args()
    import boto3  # runtime-only dependency (PYTHONPATH injection)

    s3_client = boto3.client("s3")
    run(args, s3_client)


if __name__ == "__main__":
    main()
