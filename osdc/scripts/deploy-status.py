#!/usr/bin/env python3
"""Format OSDC deploy audit log as human-readable status.

Reads kubectl ConfigMap JSON from stdin and prints current deploy state
plus recent deploy history.

Usage:
    kubectl get configmaps -n osdc-system -l ... -o json \
        | python3 deploy-status.py <cluster> [name]

    cluster: cluster ID (for display)
    name:    optional module or command name to filter
"""

import contextlib
import json
import sys

_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
NC = "\033[0m" if _TTY else ""

_PREFIX = "osdc-deploy-"
_SCOPES = {"module": 7, "cmd": 4}
_KINDS = ("start", "finish", "history")


def fmt_duration(val):
    """Format seconds string to human-readable duration."""
    try:
        s = int(val)
    except (ValueError, TypeError):
        return "-"
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    return f"{h}h{m}m" if m else f"{h}h"


def colorize_status(status):
    """Return ANSI-colored status string."""
    colors = {"completed": GREEN, "failed": RED, "started": YELLOW}
    c = colors.get(status, "")
    return f"{c}{status}{NC}" if c else status


def parse_configmaps(items):
    """Categorize deploy-log ConfigMaps into start/finish/history dicts.

    Returns (start, finish, history) where:
    - start/finish: dict of (scope, name) -> data dict
    - history: dict of (scope, name) -> list of parsed JSONL entries
    """
    start, finish, history = {}, {}, {}

    for cm in items:
        cm_name = cm["metadata"]["name"]
        data = cm.get("data", {})

        if not cm_name.startswith(_PREFIX):
            continue

        rest = cm_name[len(_PREFIX) :]

        scope = None
        for s, slen in _SCOPES.items():
            if rest.startswith(s + "-"):
                scope = s
                after = rest[slen:]
                break
        if not scope:
            continue

        for kind in _KINDS:
            if after.startswith(kind + "-"):
                name = after[len(kind) + 1 :]
                key = (scope, name)

                if kind == "history":
                    entries = []
                    for line in data.get("entries", "").strip().split("\n"):
                        if line.strip():
                            with contextlib.suppress(json.JSONDecodeError):
                                entries.append(json.loads(line))
                    history[key] = entries
                elif kind == "start":
                    start[key] = data
                else:
                    finish[key] = data
                break

    return start, finish, history


def find_in_progress(start, finish):
    """Return set of keys where start timestamp is newer than finish."""
    result = set()
    for key, sdata in start.items():
        fdata = finish.get(key, {})
        if sdata.get("timestamp", "") > fdata.get("timestamp", ""):
            result.add(key)
    return result


def print_current(start, finish, in_progress, name_filter):
    """Print current deploy state grouped by scope."""
    all_keys = sorted(set(finish.keys()) | in_progress)
    modules = []
    commands = []

    for key in all_keys:
        scope, name = key
        if name_filter and name != name_filter:
            continue

        is_running = key in in_progress

        if is_running and key in start:
            d = start[key]
            status_str = f"{YELLOW}in progress{NC}"
            duration_str = ""
        elif key in finish:
            d = finish[key]
            status_str = colorize_status(d.get("status", "unknown"))
            dur = fmt_duration(d.get("duration"))
            duration_str = dur if dur != "-" else ""
        else:
            continue

        entry = {
            "name": name,
            "status": status_str,
            "commit": d.get("commit", "?"),
            "branch": d.get("branch", "?"),
            "user": d.get("user", "?"),
            "timestamp": d.get("timestamp", "?"),
            "duration": duration_str,
            "prev": finish.get(key) if is_running and key in finish else None,
        }

        if scope == "module":
            modules.append(entry)
        else:
            commands.append(entry)

    if not modules and not commands:
        print("  No deploy records found.")
        print()
        return

    for label, items in [("Modules", modules), ("Commands", commands)]:
        if not items:
            continue
        print(f"  {BOLD}{label}{NC}")
        print()
        for e in items:
            print(f"    {BOLD}{e['name']}{NC}")
            print(f"      Status:    {e['status']}")
            print(f"      Commit:    {e['commit']} ({DIM}{e['branch']}{NC})")
            print(f"      Deployed:  {e['timestamp']}  by {e['user']}")
            if e["duration"]:
                print(f"      Duration:  {e['duration']}")
            if e["prev"]:
                p = e["prev"]
                prev_dur = fmt_duration(p.get("duration"))
                dur_suffix = f"  ({prev_dur})" if prev_dur != "-" else ""
                print(
                    f"      {DIM}Previous:  {p.get('commit', '?')}"
                    f" ({p.get('branch', '?')})"
                    f"  {p.get('timestamp', '?')}"
                    f"  by {p.get('user', '?')}{dur_suffix}{NC}"
                )
            print()


def print_history(history, name_filter, limit):
    """Print deploy history as a table."""
    targets = sorted((k, v) for k, v in history.items() if not name_filter or k[1] == name_filter)

    if not targets:
        print("  No history records found.")
        print()
        return

    for (scope, name), entries in targets:
        scope_label = "Module" if scope == "module" else "Command"

        display = list(entries) if name_filter else [e for e in entries if e.get("event") == "finish"]

        if not display:
            continue

        total = len(display)
        shown = display[-limit:]
        shown.reverse()

        header = f"  {BOLD}{scope_label}: {name}{NC}"
        if total > limit:
            header += f" {DIM}(last {limit} of {total}){NC}"
        print(header)
        print(f"    {DIM}{'TIMESTAMP':<22} {'STATUS':<11} {'COMMIT':<9} {'BRANCH':<14} {'USER':<12} {'DURATION'}{NC}")

        for e in shown:
            st = e.get("status", "?")
            ev = e.get("event", "?")
            disp = f"{ev}/{st}" if name_filter else st
            c = {"completed": GREEN, "failed": RED, "started": YELLOW}.get(st, "")
            print(
                f"    {e.get('ts', '?'):<22} {c}{disp:<11}{NC}"
                f" {e.get('commit', '?'):<9}"
                f" {e.get('branch', '?'):<14}"
                f" {e.get('user', '?'):<12}"
                f" {fmt_duration(e.get('duration'))}"
            )

        print()


def main():
    cluster = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    name_filter = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("Error: invalid JSON input", file=sys.stderr)
        sys.exit(1)

    items = data.get("items", [])
    start, finish, history = parse_configmaps(items)
    in_progress = find_in_progress(start, finish)

    print()
    print(f"{BOLD}Deploy Status: {cluster}{NC}")
    print("═" * 70)
    print()

    print(f"── Current Versions {'─' * 52}")
    print()
    print_current(start, finish, in_progress, name_filter)

    print(f"── Deploy History {'─' * 54}")
    print()
    print_history(history, name_filter, limit=20 if name_filter else 10)


if __name__ == "__main__":
    main()
