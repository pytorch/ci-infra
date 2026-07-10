#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests>=2.31"]
# ///
"""Pull pytorch/pytorch workflow_job rows from HUD ClickHouse, weekly chunks.

Hits ClickHouse Cloud directly. Reads credentials from env, in order of
precedence:

    CLICKHOUSE_URL      / CLICKHOUSE_HUD_USER_URL
    CLICKHOUSE_USER     / CLICKHOUSE_HUD_USER_USERNAME
    CLICKHOUSE_PASSWORD / CLICKHOUSE_HUD_USER_PASSWORD

Writes one JSON file per week to <out-dir>/chunk_<YYYY-MM-DD>_<YYYY-MM-DD>.json.
Each file is the raw list-of-lists returned by ClickHouse JSONCompact:
    [ [label, started_at_iso, completed_at_iso, runtime_s], ... ]

That's exactly what build_csv.py's `extract` subcommand consumes.

Usage:
    # 60 days back from now, 7-day windows
    uv run pull_hud.py --out /tmp/pytorch_workload/raw --days 60 --window-days 7

    # Explicit range
    uv run pull_hud.py --out /tmp/pytorch_workload/raw \\
        --start 2026-05-01 --end 2026-07-01 --window-days 7

    # Dry run: print the windows we WOULD fetch and skip the HTTP calls
    uv run pull_hud.py --out /tmp/pytorch_workload/raw --days 60 --dry-run

If a chunk file already exists and is non-empty, we skip it (idempotent). Pass
--force to re-fetch.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL_ENV = "CLICKHOUSE_URL"
DEFAULT_USER_ENV = "CLICKHOUSE_USER"
DEFAULT_PW_ENV = "CLICKHOUSE_PASSWORD"

FALLBACK_URL_ENV = "CLICKHOUSE_HUD_USER_URL"
FALLBACK_USER_ENV = "CLICKHOUSE_HUD_USER_USERNAME"
FALLBACK_PW_ENV = "CLICKHOUSE_HUD_USER_PASSWORD"


def _resolve_creds() -> tuple[str, str, str]:
    """Read CH creds from env with a HUD_USER_* fallback. Exits on missing."""
    url = os.environ.get(DEFAULT_URL_ENV) or os.environ.get(FALLBACK_URL_ENV)
    user = os.environ.get(DEFAULT_USER_ENV) or os.environ.get(FALLBACK_USER_ENV)
    pw = os.environ.get(DEFAULT_PW_ENV) or os.environ.get(FALLBACK_PW_ENV)
    missing = []
    if not url:
        missing.append(f"{DEFAULT_URL_ENV} / {FALLBACK_URL_ENV}")
    if not user:
        missing.append(f"{DEFAULT_USER_ENV} / {FALLBACK_USER_ENV}")
    if not pw:
        missing.append(f"{DEFAULT_PW_ENV} / {FALLBACK_PW_ENV}")
    if missing:
        print(f"ERROR: missing env vars: {'; '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    return url, user, pw


# The query is intentionally kept simple and identical across chunks so any
# schema/filter change lives in ONE place. See build_csv.py for what happens
# to the returned rows.
QUERY_TEMPLATE = """
SELECT
    label,
    toString(started_at, 'UTC') AS started_at_s,
    toString(completed_at, 'UTC') AS completed_at_s,
    runtime_s
FROM (
    SELECT
        started_at,
        completed_at,
        dateDiff('second', started_at, completed_at) AS runtime_s,
        arrayFilter(x -> startsWith(x, 'mt-') OR startsWith(x, 'lf-'), labels) AS mt_lf_labels
    FROM default.workflow_job
    WHERE started_at >= toDateTime64('{start}', 3, 'UTC')
      AND started_at <  toDateTime64('{end}', 3, 'UTC')
      AND repository_full_name = 'pytorch/pytorch'
      AND status = 'completed'
      AND conclusion != ''
      AND arrayExists(x -> startsWith(x, 'mt-') OR startsWith(x, 'lf-'), labels)
      AND dateDiff('second', started_at, completed_at) BETWEEN 1 AND 21600
)
ARRAY JOIN mt_lf_labels AS label
FORMAT JSONCompact
"""


def _iso_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def _windows(start: dt.date, end: dt.date, window_days: int) -> list[tuple[dt.date, dt.date]]:
    """Return [(win_start, win_end), ...] non-overlapping, half-open [start, end)."""
    if window_days <= 0:
        raise ValueError("window-days must be positive")
    out = []
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=window_days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def _chunk_path(out_dir: Path, win_start: dt.date, win_end: dt.date) -> Path:
    return out_dir / f"chunk_{win_start.isoformat()}_{win_end.isoformat()}.json"


def _fmt_ts(d: dt.date) -> str:
    """Format a date as 'YYYY-MM-DD HH:MM:SS.000' for toDateTime64() in CH."""
    return f"{d.isoformat()} 00:00:00.000"


def _run_query(url: str, user: str, password: str, sql: str, timeout_s: int) -> dict:
    """POST the query and return the parsed JSON body."""
    r = requests.post(
        url,
        params={"database": "default"},
        data=sql,
        auth=(user, password),
        timeout=timeout_s,
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    r.raise_for_status()
    return r.json()


def _extract_rows(payload: dict) -> list[list]:
    """ClickHouse JSONCompact -> plain list of rows.

    JSONCompact returns:
        {"meta":[...], "data":[[...],[...]], "rows":N, "statistics":{...}}
    """
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError(f"unexpected response shape: keys={list(payload)}")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="output directory for chunk files")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--days", type=int, default=60, help="days back from today (default 60)")
    ap.add_argument("--start", type=_iso_date, help="explicit start date YYYY-MM-DD (UTC)")
    ap.add_argument("--end", type=_iso_date, help="explicit end date YYYY-MM-DD (UTC, exclusive)")
    ap.add_argument("--window-days", type=int, default=7, help="chunk size (default 7)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if chunk exists")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--timeout", type=int, default=300, help="per-request timeout in seconds")
    ap.add_argument("--sleep", type=float, default=0.0, help="sleep between chunks (throttle)")
    args = ap.parse_args()

    today = dt.datetime.now(dt.UTC).date()
    if args.start and args.end:
        start, end = args.start, args.end
    elif args.start or args.end:
        print("ERROR: --start and --end must be given together", file=sys.stderr)
        return 2
    else:
        end = today
        start = end - dt.timedelta(days=args.days)
    if start >= end:
        print(f"ERROR: start {start} >= end {end}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    wins = _windows(start, end, args.window_days)
    print(f"plan: {len(wins)} weekly chunk(s) covering [{start}, {end})", file=sys.stderr)
    for a, b in wins:
        print(f"  {a} -> {b}   ({_chunk_path(out_dir, a, b).name})", file=sys.stderr)

    if args.dry_run:
        return 0

    url, user, pw = _resolve_creds()

    for a, b in wins:
        path = _chunk_path(out_dir, a, b)
        if path.exists() and path.stat().st_size > 0 and not args.force:
            print(f"skip (exists): {path.name}", file=sys.stderr)
            continue
        sql = QUERY_TEMPLATE.format(start=_fmt_ts(a), end=_fmt_ts(b))
        t0 = time.monotonic()
        try:
            payload = _run_query(url, user, pw, sql, args.timeout)
        except requests.HTTPError as e:
            body = e.response.text[:500] if e.response is not None else ""
            print(
                f"ERROR: {path.name}: HTTP {e.response.status_code if e.response is not None else '?'}: {body}",
                file=sys.stderr,
            )
            return 3
        rows = _extract_rows(payload)
        path.write_text(json.dumps(rows))
        dt_s = time.monotonic() - t0
        print(f"wrote {path.name}   rows={len(rows):>7d}   {dt_s:.1f}s", file=sys.stderr)
        if args.sleep > 0:
            time.sleep(args.sleep)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
