#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["huggingface_hub>=0.24"]
# ///
"""Refresh the shared HuggingFace model cache.

Downloads a curated set of model repos from the HuggingFace Hub into a local
HF cache directory, then publishes a *symlink-free* copy to the shared S3
bucket. Runners mount that bucket read-only at /mnt/hf_cache and read models
offline (HF_HUB_OFFLINE=1), so this job is the only writer.

Why symlink-free: the default HF cache stores snapshots/<rev>/<file> as symlinks
into blobs/<sha>. Object storage has no symlinks, so we dereference them on the
way out (`rclone copy -L`) and drop the now-redundant blobs/, leaving real files
under snapshots/. That keeps `from_pretrained("org/model")` working transparently
without needing a POSIX metadata layer over S3.

Run locally:  uv run refresh_cache.py --models models.txt --bucket B --region R
In-cluster:   driven by the refresh CronJob (see kubernetes/refresh-cronjob.yaml.tpl)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_manifest(text: str) -> list[tuple[str, str | None]]:
    """Parse a model manifest into (repo_id, revision) pairs.

    Format: one entry per line, ``repo_id`` or ``repo_id@revision``. Blank lines
    and ``#`` comments (full-line or trailing) are ignored.

    >>> parse_manifest("# header\\nmeta-llama/Llama-3.1-8B\\nopenai/clip@abc123  # pin\\n")
    [('meta-llama/Llama-3.1-8B', None), ('openai/clip', 'abc123')]
    """
    entries: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "@" in line:
            repo_id, revision = line.split("@", 1)
            entries.append((repo_id.strip(), revision.strip() or None))
        else:
            entries.append((line, None))
    return entries


def download_models(
    entries: list[tuple[str, str | None]],
    cache_dir: Path,
    token: str | None,
) -> None:
    """Download each manifest entry into the HF cache layout at *cache_dir*."""
    # Imported lazily so the module (and its unit tests) load without the
    # huggingface_hub dependency present.
    from huggingface_hub import snapshot_download

    cache_dir.mkdir(parents=True, exist_ok=True)
    for repo_id, revision in entries:
        label = repo_id if revision is None else f"{repo_id}@{revision}"
        print(f"[hf-cache] downloading {label} ...", flush=True)
        snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=str(cache_dir),
            token=token,
        )
    print(f"[hf-cache] downloaded {len(entries)} repo(s) into {cache_dir}", flush=True)


def build_rclone_args(
    cache_dir: Path,
    bucket: str,
    region: str,
    prefix: str,
    prune: bool,
) -> list[str]:
    """Build the rclone command that publishes *cache_dir* to S3.

    ``-L`` dereferences HF's snapshot symlinks into real objects; ``blobs/`` is
    excluded because those bytes are duplicated into snapshots by ``-L``. Uses
    ``copy`` (additive) by default; ``sync`` (mirrors, deletes de-listed models)
    only when *prune* is set, since a partial download must never delete S3 data.

    >>> build_rclone_args(Path("/work/hub"), "b", "us-east-2", "hub", False)[:2]
    ['rclone', 'copy']
    """
    remote = f":s3,provider=AWS,env_auth=true,region={region}:{bucket}/{prefix}"
    return [
        "rclone",
        "sync" if prune else "copy",
        "-L",
        "--exclude",
        "blobs/**",
        "--transfers",
        "8",
        "--checkers",
        "16",
        "--fast-list",
        str(cache_dir),
        remote,
    ]


def publish(
    cache_dir: Path,
    bucket: str,
    region: str,
    prefix: str,
    prune: bool,
    dry_run: bool,
) -> None:
    """Publish the local cache to S3 via rclone."""
    args = build_rclone_args(cache_dir, bucket, region, prefix, prune)
    if dry_run:
        args.append("--dry-run")
    print(f"[hf-cache] publishing: {' '.join(args)}", flush=True)
    subprocess.run(args, check=True)
    print("[hf-cache] publish complete", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the shared HuggingFace model cache")
    parser.add_argument("--models", required=True, type=Path, help="Path to the model manifest")
    parser.add_argument("--bucket", required=True, help="Target S3 bucket")
    parser.add_argument("--region", required=True, help="S3 bucket region")
    parser.add_argument("--cache-dir", required=True, type=Path, help="Local HF cache dir (the 'hub' dir)")
    parser.add_argument("--prefix", default="hub", help="Key prefix within the bucket (default: hub)")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Mirror with rclone sync (deletes de-listed models). Default is additive copy.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Download but do not write to S3")

    args = parser.parse_args(argv)

    entries = parse_manifest(args.models.read_text())
    if not entries:
        print(f"[hf-cache] no models listed in {args.models}; nothing to do", file=sys.stderr)
        return 0

    token = os.environ.get("HF_TOKEN") or None
    download_models(entries, args.cache_dir, token)
    publish(args.cache_dir, args.bucket, args.region, args.prefix, args.prune, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
