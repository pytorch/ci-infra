#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Stdin-to-file log rotator with date-based rotation.

Reads lines from stdin (piped from pypiserver), echoes each to stdout,
and writes to date-stamped files with automatic daily rotation and
cleanup of old logs.
"""

from __future__ import annotations

import argparse
import datetime
import signal
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Stdin-to-file log rotator with date-based rotation")
    parser.add_argument("--log-dir", required=True, help="Directory to write log files")
    parser.add_argument(
        "--max-age-days", type=int, default=30, help="Remove log files older than this many days (default: 30)"
    )
    parser.add_argument("--prefix", default="access", help="Log filename prefix (default: access)")
    return parser.parse_args(argv)


def log_filename(prefix: str, date: datetime.date) -> str:
    """Return the log filename for a given date."""
    return f"{prefix}.{date.isoformat()}.log"


def cleanup_old_logs(log_dir: Path, prefix: str, max_age_days: int) -> None:
    """Remove log files older than max_age_days."""
    if not log_dir.is_dir():
        return
    cutoff = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=max_age_days)  # noqa: UP017 — datetime.UTC requires Python 3.11+, pypiserver image runs 3.9
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        # Match pattern: <prefix>.YYYY-MM-DD.log
        if not (name.startswith(f"{prefix}.") and name.endswith(".log")):
            continue
        date_str = name[len(f"{prefix}.") : -len(".log")]
        try:
            file_date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        if file_date < cutoff:
            entry.unlink()


def run(args: argparse.Namespace, stdin=None, stdout=None, now_fn=None) -> None:
    """Main loop: read stdin, echo to stdout, write to date-stamped log files."""
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    if now_fn is None:
        now_fn = lambda: datetime.datetime.now(datetime.timezone.utc)  # noqa: E731, UP017 — datetime.UTC requires Python 3.11+

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    shutdown = False

    def handle_sigterm(_signum, _frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGTERM, handle_sigterm)

    # Initial cleanup
    cleanup_old_logs(log_dir, args.prefix, args.max_age_days)

    current_date = now_fn().date()
    log_path = log_dir / log_filename(args.prefix, current_date)
    log_file = open(log_path, "a")  # noqa: SIM115

    try:
        for line in stdin:
            if shutdown:
                break

            stdout.write(line)
            stdout.flush()

            today = now_fn().date()
            if today != current_date:
                log_file.close()
                cleanup_old_logs(log_dir, args.prefix, args.max_age_days)
                current_date = today
                log_path = log_dir / log_filename(args.prefix, current_date)
                log_file = open(log_path, "a")  # noqa: SIM115

            log_file.write(line)
            log_file.flush()
    finally:
        stdout.flush()
        log_file.flush()
        log_file.close()


def main() -> None:
    """Entry point."""
    args = parse_args()
    run(args)


if __name__ == "__main__":
    main()
