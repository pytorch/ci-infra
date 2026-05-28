#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Validate a Renovate-style runner_image_tag bump patch.

Defense-in-depth gate shared by osdc-renovate-autoapprove.yml (pre-merge,
operates on PR file list) and osdc-auto-update-deploy-prod.yml (post-merge,
operates on the merge commit). Keeping bit-for-bit identical regex,
line-count, and monotonicity rules in one place avoids drift between the
two enforcement points.

Inputs (env vars):
    PATCH_JSON  JSON from `gh api repos/X/pulls/N/files` (top-level array)
                OR `gh api repos/X/commits/SHA` (object with .files).
                Shape is auto-detected: array => PR form; object => commit form.

Outputs (stdout, KEY=VALUE lines):
    decision=approve | close-wrong-file-count | close-wrong-file |
             close-no-patch | close-multi-line | close-bad-pattern |
             close-no-change | close-downgrade
    reason=<human-readable explanation>
    old_ver=<X.Y.Z>  (only when decision=approve)
    new_ver=<X.Y.Z>  (only when decision=approve)

Exit code: 0 on every deterministic decision (callers branch on `decision`).
Non-zero only for unrecoverable errors (missing PATCH_JSON, malformed JSON).
"""

import json
import os
import re
import sys

EXPECTED_FILE = "osdc/clusters.yaml"

LINE_PATTERN = re.compile(r'^    runner_image_tag:[ \t]*"(?P<ver>\d+\.\d+\.\d+)"[ \t]*(#.*)?$')


def _emit(decision: str, reason: str, **kwargs: str) -> None:
    fields = {"decision": decision, "reason": reason, **kwargs}
    print(*(f"{k}={v}" for k, v in fields.items()), sep="\n")


def _semver_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def main() -> int:
    raw = os.environ.get("PATCH_JSON")
    if not raw:
        print("validate-runner-bump.py: PATCH_JSON env var is required", file=sys.stderr)
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"validate-runner-bump.py: PATCH_JSON is not valid JSON: {e}", file=sys.stderr)
        return 2

    # Auto-detect shape: top-level array = PR-files form; object = commit form.
    if isinstance(payload, list):
        files = payload
    elif isinstance(payload, dict):
        files = payload.get("files") or []
    else:
        print(
            f"validate-runner-bump.py: PATCH_JSON must be array or object, got {type(payload).__name__}",
            file=sys.stderr,
        )
        return 2

    if len(files) != 1:
        _emit("close-wrong-file-count", f"expected exactly 1 file changed; got {len(files)}")
        return 0

    file_path = files[0].get("filename", "")
    if file_path != EXPECTED_FILE:
        _emit("close-wrong-file", f"expected only {EXPECTED_FILE} to change; got {file_path}")
        return 0

    patch = files[0].get("patch")
    if not patch:
        _emit(
            "close-no-patch",
            "GitHub returned no patch for this file (likely binary, rename-only, or oversized)",
        )
        return 0

    # Diff lines: include +/- markers; exclude hunk headers (++/--) and context.
    added = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("++")]
    removed = [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("--")]
    if len(added) != 1 or len(removed) != 1:
        _emit(
            "close-multi-line",
            f"expected exactly 1 line added + 1 line removed; got +{len(added)}/-{len(removed)}",
        )
        return 0

    new_line = added[0]
    old_line = removed[0]

    new_match = LINE_PATTERN.match(new_line)
    if not new_match:
        _emit("close-bad-pattern", f"new line does not match runner_image_tag semver pattern: {new_line}")
        return 0
    old_match = LINE_PATTERN.match(old_line)
    if not old_match:
        _emit("close-bad-pattern", f"old line does not match runner_image_tag semver pattern: {old_line}")
        return 0

    old_ver = old_match.group("ver")
    new_ver = new_match.group("ver")

    if old_ver == new_ver:
        _emit("close-no-change", f"no version change ({old_ver} -> {new_ver})")
        return 0

    # Monotonicity: reject downgrades (defense against a compromised RENOVATE_TOKEN
    # rolling back to a known-vulnerable runner version).
    if _semver_tuple(new_ver) < _semver_tuple(old_ver):
        _emit("close-downgrade", f"downgrade detected ({old_ver} -> {new_ver})")
        return 0

    _emit("approve", f"{old_ver} -> {new_ver}", old_ver=old_ver, new_ver=new_ver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
