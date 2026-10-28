#!/usr/bin/env python3
"""Clean up detached EBS volumes according to a per-account policy.

Dry run by default: prints and reports what it would do. Pass --execute to act.
See README.md for the rules and safety guarantees.
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from rules import (
    FIRST_SEEN_TAG,
    Action,
    Context,
    Decision,
    Policy,
    Volume,
    classify,
    is_expired_janitor_snapshot,
    list_price_month,
)

MUTATING = (Action.DELETE, Action.SNAPSHOT, Action.TAG_FIRST_SEEN)


@dataclass
class Budget:
    deletes: int
    snapshots: int
    snapshot_deletes: int

    def take(self, action: Action) -> bool:
        if action == Action.DELETE:
            if self.deletes <= 0:
                return False
            self.deletes -= 1
        elif action == Action.SNAPSHOT:
            if self.snapshots <= 0:
                return False
            self.snapshots -= 1
        return True


@dataclass
class Row:
    region: str
    resource_id: str
    kind: str
    size_gib: int
    created: str
    rule: str
    action: str
    result: str
    detail: str = ""
    list_usd_month: float = 0.0


@dataclass
class RegionResult:
    region: str
    rows: list[Row] = field(default_factory=list)
    aborted: str = ""


def _row(v: Volume, d: Decision, result: str) -> Row:
    # Never put tag values in a row: gpu-dev tags hold user emails and the
    # report is published in a public repo's workflow logs.
    return Row(
        region=v.region,
        resource_id=v.id,
        kind="volume",
        size_gib=v.size,
        created=v.created.date().isoformat(),
        rule=d.rule,
        action=d.action.value,
        result=result,
        detail=d.detail,
        list_usd_month=round(list_price_month(v), 2),
    )


def process_region(client, policy: Policy, now: datetime, execute: bool, budget: Budget) -> RegionResult:
    res = RegionResult(client.region)
    volumes = client.detached_volumes()

    decisions: list[tuple[Volume, Decision]] = []
    if volumes:
        ctx = Context(
            now=now,
            ami_names=client.ami_names({v.snapshot_id for v in volumes}, policy.runner_ami_owners),
            snapshots_by_volume=client.snapshots_by_volume({v.id for v in volumes}),
            live_clusters=client.live_clusters(),
        )
        decisions = [(v, classify(v, ctx, policy)) for v in volumes]

    actionable = [(v, d) for v, d in decisions if d.action in MUTATING]
    for v, d in decisions:
        if d.action not in MUTATING:
            res.rows.append(_row(v, d, d.action.value))

    if not execute:
        res.rows.extend(_row(v, d, "planned") for v, d in actionable)
        for _, row in _expired_snapshots(client, now):
            row.result = "planned"
            res.rows.append(row)
        return res

    if actionable:
        denied = client.preflight(actionable[0][0].id)
        if denied:
            res.aborted = "permission preflight failed: " + ", ".join(denied)
            res.rows.extend(_row(v, d, "not run") for v, d in actionable)
            return res

    current = client.recheck([v for v, _ in actionable])
    attempted = errors = 0
    for v, d in actionable:
        now_v = current.get(v.id)
        if now_v is None:
            res.rows.append(_row(v, d, "skipped: no longer detached"))
            continue
        if now_v.tags != v.tags:
            res.rows.append(_row(v, d, "skipped: tags changed"))
            continue
        if not budget.take(d.action):
            res.rows.append(_row(v, d, "skipped: per-run cap reached"))
            continue
        attempted += 1
        try:
            if d.action == Action.DELETE:
                client.delete_volume(v.id)
                result = "deleted"
            elif d.action == Action.SNAPSHOT:
                result = "snapshot " + client.snapshot(v, d.rule, now, policy.snapshot_retention_days)
            else:
                client.tag(v.id, {FIRST_SEEN_TAG: now.date().isoformat()})
                result = "tagged"
        except Exception as e:  # noqa: BLE001 - log, count and keep going
            errors += 1
            result = f"error: {type(e).__name__}: {e}"[:300]
        res.rows.append(_row(v, d, result))
        if errors > max(5, attempted // 20):
            res.aborted = f"aborted after {errors} errors in {attempted} actions"
            break

    if not res.aborted:
        for snap, row in _expired_snapshots(client, now):
            if budget.snapshot_deletes <= 0:
                row.result = "skipped: per-run cap reached"
            else:
                budget.snapshot_deletes -= 1
                try:
                    client.delete_snapshot(snap)
                    row.result = "deleted"
                except Exception as e:  # noqa: BLE001
                    row.result = f"error: {type(e).__name__}: {e}"[:300]
            res.rows.append(row)
    return res


def _expired_snapshots(client, now: datetime) -> list[tuple]:
    out = []
    for s in client.janitor_snapshots():
        if is_expired_janitor_snapshot(s, now):
            row = Row(
                region=client.region,
                resource_id=s.id,
                kind="snapshot",
                size_gib=0,
                created=s.start_time.date().isoformat(),
                rule="janitor-snapshot-expired",
                action=Action.DELETE.value,
                result="",
            )
            out.append((s, row))
    return out


def summarize(results: list[RegionResult], execute: bool) -> str:
    agg: dict[tuple, list] = collections.defaultdict(lambda: [0, 0, 0.0])
    outcomes: dict[tuple, collections.Counter] = collections.defaultdict(collections.Counter)
    for r in results:
        for row in r.rows:
            key = (row.region, row.rule, row.action)
            a = agg[key]
            a[0] += 1
            a[1] += row.size_gib
            a[2] += row.list_usd_month
            outcomes[key][row.result.split(" ")[0].rstrip(":")] += 1
    mode = "execute" if execute else "dry run"
    lines = [
        f"## EBS janitor ({mode})",
        "",
        "| Region | Rule | Action | Count | GiB | Est. list $/month | Results |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for key in sorted(agg, key=lambda k: (k[0], -agg[k][2])):
        n, gib, usd = agg[key]
        res = ", ".join(f"{k} {v}" for k, v in outcomes[key].most_common())
        lines.append(f"| {key[0]} | {key[1]} | {key[2]} | {n:,} | {gib:,} | {usd:,.0f} | {res} |")
    aborted = [r for r in results if r.aborted]
    if aborted:
        lines += ["", "**Aborted regions:**", *[f"- {r.region}: {r.aborted}" for r in aborted]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True, type=Path, help="policy YAML, see policies/")
    p.add_argument("--regions", nargs="*", help="override the policy's region list")
    p.add_argument("--execute", action="store_true", help="act; without it this is a dry run")
    p.add_argument("--max-deletes", type=int, default=500, help="volume deletes per run")
    p.add_argument("--max-snapshots", type=int, default=200, help="janitor snapshots created per run")
    p.add_argument("--max-snapshot-deletes", type=int, default=500, help="expired janitor snapshots deleted per run")
    p.add_argument("--report-dir", type=Path, default=Path("ebs-janitor-report"))
    args = p.parse_args(argv)

    import aws  # imported late so the rules and tests don't need boto3

    policy = Policy.from_dict(yaml.safe_load(args.config.read_text()) or {})
    regions = args.regions or policy.regions or aws.enabled_regions()
    regions = [r for r in regions if r not in policy.exclude_regions]
    now = datetime.now(UTC)
    budget = Budget(args.max_deletes, args.max_snapshots, args.max_snapshot_deletes)

    results = []
    for region in regions:
        print(f"== {region}", flush=True)
        r = process_region(aws.RegionClient(region), policy, now, args.execute, budget)
        for row in r.rows:
            print(
                f"{row.region} {row.resource_id} {row.size_gib}GiB {row.rule} {row.action} -> {row.result} {row.detail}"
            )
        if r.aborted:
            print(f"!! {region}: {r.aborted}", flush=True)
        results.append(r)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    with open(args.report_dir / "report.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "region",
                "resource_id",
                "kind",
                "size_gib",
                "created",
                "rule",
                "action",
                "result",
                "detail",
                "list_usd_month",
            ]
        )
        for r in results:
            for row in r.rows:
                w.writerow(
                    [
                        row.region,
                        row.resource_id,
                        row.kind,
                        row.size_gib,
                        row.created,
                        row.rule,
                        row.action,
                        row.result,
                        row.detail,
                        row.list_usd_month,
                    ]
                )
    summary = summarize(results, args.execute)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary)
    return 1 if any(r.aborted for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
