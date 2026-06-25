#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["huggingface_hub>=0.24", "pyyaml>=6.0"]
# ///
"""Seed HuggingFace models into OSDC clusters' hf-cache S3 buckets.

Each model is downloaded once locally, then ``aws s3 sync``ed to every target
cluster's per-region bucket ``pytorch-hf-model-cache-<cluster_id>`` (the layout
the runners read at ``/mnt/hf_cache/hub``). Target one or more clusters, or
``--all`` (every cluster that enables the hf-cache module); clusters are synced
in parallel. Writes straight to S3 — independent of the mount on the cluster.

Requires ``aws`` on PATH (run with mise active, e.g. from the osdc/ dir).

Usage:
  uv run scripts/hf-cache-seed.py -c meta-staging-aws-ue1 Qwen/Qwen2.5-7B-Instruct
  uv run scripts/hf-cache-seed.py --all Qwen/Qwen2.5-7B-Instruct meta-llama/Llama-3.1-8B
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

BUCKET_PREFIX = "pytorch-hf-model-cache-"
HF_MODULE = "hf-cache"
CLUSTERS_YAML = Path(os.environ.get("CLUSTERS_YAML", Path(__file__).resolve().parent.parent / "clusters.yaml"))


def load_clusters() -> dict:
    with open(CLUSTERS_YAML) as f:
        return yaml.safe_load(f).get("clusters", {}) or {}


def resolve_targets(clusters: dict, requested: list[str] | None, all_clusters: bool) -> list[str]:
    """Return the cluster ids to seed. --all selects every hf-cache-enabled cluster."""
    if all_clusters:
        targets = [cid for cid, c in clusters.items() if HF_MODULE in (c.get("modules") or [])]
        if not targets:
            raise SystemExit(f"No clusters enable the '{HF_MODULE}' module in {CLUSTERS_YAML}")
        return targets
    targets = []
    for cid in requested or []:
        if cid not in clusters:
            raise SystemExit(f"Unknown cluster '{cid}'. Known: {', '.join(clusters)}")
        if HF_MODULE not in (clusters[cid].get("modules") or []):
            print(
                f"WARNING: cluster '{cid}' does not enable '{HF_MODULE}' — its bucket may not exist.", file=sys.stderr
            )
        targets.append(cid)
    return targets


def bucket_for(cid: str) -> str:
    return f"{BUCKET_PREFIX}{cid}"


def download_models(models: list[str], staging: Path) -> None:
    """Download each model once into staging/hub (shared across all target buckets)."""
    from huggingface_hub import snapshot_download

    hub = staging / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    for model in models:
        print(f"-> downloading {model} ...", flush=True)
        path = snapshot_download(model, cache_dir=str(hub))
        print(f"   {model} -> {path}", flush=True)


def sync_to_cluster(cid: str, region: str, staging: Path) -> tuple[str, bool, str]:
    cmd = [
        "aws",
        "s3",
        "sync",
        str(staging / "hub"),
        f"s3://{bucket_for(cid)}/hub",
        "--region",
        region,
        "--no-progress",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    return cid, ok, (proc.stdout if ok else (proc.stderr or proc.stdout)).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed HF models into OSDC hf-cache S3 buckets.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-c", "--cluster", action="append", metavar="CLUSTER_ID", help="target cluster (repeatable)")
    group.add_argument("--all", action="store_true", help="target every cluster that enables the hf-cache module")
    parser.add_argument("models", nargs="+", help="HF model id(s), e.g. Qwen/Qwen2.5-7B-Instruct")
    parser.add_argument("-j", "--jobs", type=int, default=0, help="parallel cluster syncs (default: one per cluster)")
    args = parser.parse_args(argv)

    clusters = load_clusters()
    targets = resolve_targets(clusters, args.cluster, args.all)
    print(f"Targets ({len(targets)}): {', '.join(targets)}")
    print(f"Models  ({len(args.models)}): {', '.join(args.models)}")

    if shutil.which("aws") is None:
        raise SystemExit("ERROR: 'aws' not found on PATH — run with mise active (e.g. from the osdc/ dir).")

    staging = Path(tempfile.mkdtemp(prefix="hf-cache-seed-"))
    try:
        download_models(args.models, staging)
        workers = args.jobs or len(targets)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(sync_to_cluster, cid, clusters[cid]["region"], staging) for cid in targets]
            results = [f.result() for f in concurrent.futures.as_completed(futs)]
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print("\n=== results ===")
    failed = 0
    for cid, ok, detail in sorted(results):
        if ok:
            print(f"  OK   {cid}  -> s3://{bucket_for(cid)}/hub")
        else:
            failed += 1
            tail = "\n        ".join(detail.splitlines()[-5:])
            print(f"  FAIL {cid}\n        {tail}")
    if failed:
        print(f"\n{failed}/{len(results)} cluster(s) failed.")
        return 1
    print(f"\nSeeded {len(args.models)} model(s) into {len(results)} cluster(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
