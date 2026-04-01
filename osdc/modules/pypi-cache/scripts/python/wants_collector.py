#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Wants-collector daemon: scans pypiserver access logs, filters against PyPI, uploads wants list to S3.

Runs as a long-lived pod. Each cycle:
1. Scans EFS access logs to find requested packages
2. Downloads a prebuilt cache from S3 (shared across clusters)
3. Checks PyPI JSON API for pre-built wheel availability
4. Uploads filtered wants list (only packages needing building) to S3
5. Updates the shared prebuilt cache
6. Sleeps and repeats
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import signal
import time
import urllib.error
import urllib.request
from pathlib import Path

# PEP 503 normalization: lowercase, collapse [-_.] runs to single hyphen
RE_NORMALIZE = re.compile(r"[-_.]+")

# Match GET /simple/<package>/ — captures package name
RE_SIMPLE = re.compile(r"GET /simple/([^/]+)/")

# Match GET /<slug>/<package>-<version>(-<...>).(whl|tar.gz|zip) — captures package, version
RE_DOWNLOAD = re.compile(
    r"GET /\S+/([A-Za-z0-9_.-]+)-(\d+(?:\.\d+)*(?:\.(?:post|dev|a|b|rc)\d*)*)[-.]+[^/]*\.(?:whl|tar\.gz|zip)"
)

# Legacy manylinux aliases → glibc version tuples
MANYLINUX_ALIASES: dict[str, tuple[int, int]] = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}


def _normalize_name(name: str) -> str:
    """PEP 503 package name normalization."""
    return RE_NORMALIZE.sub("-", name).lower()


def parse_log_line(line: str) -> tuple[str, str] | None:
    """Extract (normalized_package, version) from a log line, or None."""
    m = RE_DOWNLOAD.search(line)
    if m:
        return _normalize_name(m.group(1)), m.group(2)
    m = RE_SIMPLE.search(line)
    if m:
        return (_normalize_name(m.group(1)), "")
    return None


def _parse_file(log_file: Path, results: set[tuple[str, str]]) -> None:
    """Parse a single log file, adding (package, version) tuples to results."""
    try:
        with open(log_file) as f:
            for line in f:
                parsed = parse_log_line(line)
                if parsed and parsed[1]:
                    results.add(parsed)
    except OSError:
        pass


def scan_logs(log_dir: Path) -> set[tuple[str, str]]:
    """Read all .log files in log_dir, parse for package downloads."""
    results: set[tuple[str, str]] = set()
    if not log_dir.is_dir():
        return results
    for log_file in sorted(log_dir.iterdir()):
        if log_file.is_file() and log_file.name.endswith(".log"):
            _parse_file(log_file, results)
    return results


def cleanup_old_logs(log_dir: Path, max_age_days: int) -> None:
    """Delete fallback log files older than max_age_days.

    Expects filenames like fallback.YYYY-MM-DD.log (daily rotation by nginx).
    Skips non-date files (e.g. fallback.date-unknown.log).
    """
    if not log_dir.is_dir():
        return
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=max_age_days)
    for f in sorted(log_dir.iterdir()):
        if not f.is_file() or not f.name.startswith("fallback.") or not f.name.endswith(".log"):
            continue
        # Extract date from fallback.YYYY-MM-DD.log
        stem = f.name[len("fallback.") : -len(".log")]
        try:
            file_date = datetime.date.fromisoformat(stem)
        except ValueError:
            continue  # skip non-date files like fallback.date-unknown.log
        if file_date < cutoff:
            try:
                f.unlink()
                print(f"Deleted old log: {f.name}")
            except OSError:
                pass


def build_matrix(
    python_versions: list[str], architectures: list[str], manylinux: str
) -> tuple[str, set[tuple[str, str]]]:
    """Build the target matrix.

    Returns (header_string, set of (cpXY, arch) tuples).
    Header format: "py3.10,py3.11 x86_64,aarch64 manylinux_2_17"
    """
    py_tags = [f"py{v}" for v in python_versions]
    header = f"{','.join(py_tags)} {','.join(architectures)} manylinux_{manylinux}"
    combos: set[tuple[str, str]] = set()
    for ver in python_versions:
        cp = f"cp{ver.replace('.', '')}"
        for arch in architectures:
            combos.add((cp, arch))
    return header, combos


def is_manylinux_compatible(platform_tag: str, target_manylinux: str) -> tuple[bool, str | None]:
    """Check if a wheel platform tag is compatible with the target manylinux.

    Returns (is_compatible, arch_or_None).
    """
    target_parts = target_manylinux.split("_")
    target_version = (int(target_parts[0]), int(target_parts[1]))

    for alias, version in MANYLINUX_ALIASES.items():
        if alias in platform_tag:
            arch = platform_tag.split(alias)[-1].lstrip("_")
            return version <= target_version, arch or None

    m = re.search(r"manylinux_(\d+)_(\d+)_(\w+)", platform_tag)
    if m:
        wheel_version = (int(m.group(1)), int(m.group(2)))
        return wheel_version <= target_version, m.group(3)

    return False, None


def check_pypi(
    package: str, version: str, matrix_combos: set[tuple[str, str]], target_manylinux: str, http_get
) -> bool:
    """Check PyPI for pre-built wheels. Returns True if the package needs building.

    Raises on network errors (caller should abort the cycle).
    Returns False for 404 (package not on PyPI — skip it).
    """
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    try:
        status, body = http_get(url)
    except Exception:
        raise
    if status == 404:
        return False

    data = json.loads(body)
    urls = data.get("urls", [])

    for entry in urls:
        if entry.get("packagetype") == "bdist_wheel":
            filename = entry.get("filename", "")
            if filename.endswith("-py3-none-any.whl") or "-none-any" in filename:
                return False

    covered: set[tuple[str, str]] = set()
    for entry in urls:
        if entry.get("packagetype") != "bdist_wheel":
            continue
        filename = entry.get("filename", "")
        parts = filename.rsplit(".", 1)[0].split("-") if filename.endswith(".whl") else []
        if len(parts) < 5:
            continue
        python_tag = parts[-3]
        platform_tag = parts[-1]

        compatible, arch = is_manylinux_compatible(platform_tag, target_manylinux)
        if not compatible or not arch:
            continue

        for cp, target_arch in matrix_combos:
            if arch == target_arch and cp in python_tag:
                covered.add((cp, target_arch))

    return covered != matrix_combos


def filter_packages(
    packages: set[tuple[str, str]],
    matrix_combos: set[tuple[str, str]],
    target_manylinux: str,
    prebuilt: set[str],
    http_get,
    needbuild: set[str] | None = None,
) -> tuple[set[str], set[str]]:
    """Filter packages: returns (wants_set, updated_prebuilt_set).

    Packages whose name appears in needbuild are added to wants unconditionally,
    bypassing both the prebuilt cache and the PyPI availability check.

    Raises on network errors (caller aborts the cycle).
    """
    wants: set[str] = set()
    needbuild = needbuild or set()
    updated_prebuilt = set(prebuilt)
    checked = 0
    for pkg, ver in sorted(packages):
        entry = f"{pkg}=={ver}"
        if pkg in needbuild:
            wants.add(entry)
            continue
        if entry in updated_prebuilt:
            continue
        if checked > 0:
            time.sleep(0.1)
        needs_build = check_pypi(pkg, ver, matrix_combos, target_manylinux, http_get)
        checked += 1
        if needs_build:
            wants.add(entry)
        else:
            updated_prebuilt.add(entry)
    return wants, updated_prebuilt


def format_wants(packages: set[str]) -> str:
    """Format wants set as sorted, newline-terminated string."""
    return "\n".join(sorted(packages)) + "\n" if packages else ""


def parse_prebuilt_cache(content: str | None, expected_header: str) -> set[str]:
    """Parse prebuilt cache content. Returns empty set if header mismatches or content is None."""
    if not content:
        return set()
    lines = content.strip().splitlines()
    if not lines:
        return set()
    first = lines[0]
    if not first.startswith("# matrix: "):
        return set()
    header = first[len("# matrix: ") :]
    if header != expected_header:
        print(f"Prebuilt cache matrix mismatch: got '{header}', expected '{expected_header}' — invalidating cache")
        return set()
    return {line.strip() for line in lines[1:] if line.strip()}


def parse_needbuild(content: str | None) -> set[str]:
    """Parse needbuild.txt content. Returns set of PEP 503-normalized package names.

    Lines starting with # are comments. Blank lines are skipped.
    """
    if not content:
        return set()
    result: set[str] = set()
    for line in content.strip().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            result.add(_normalize_name(stripped))
    return result


def format_prebuilt_cache(header: str, entries: set[str]) -> str:
    """Format prebuilt cache as header + sorted entries."""
    lines = [f"# matrix: {header}"]
    lines.extend(sorted(entries))
    return "\n".join(lines) + "\n"


def download_from_s3(bucket: str, key: str, s3_client) -> str | None:
    """Download a file from S3. Returns content string or None if not found."""
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except s3_client.exceptions.NoSuchKey:
        return None


def upload_to_s3(content: str, bucket: str, key: str, s3_client) -> None:
    """Upload content string to S3."""
    s3_client.put_object(Bucket=bucket, Key=key, Body=content.encode("utf-8"), ContentType="text/plain")


def _default_http_get(url: str) -> tuple[int, str]:
    """Default HTTP GET using urllib."""
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Scan pypiserver access logs and upload wants list to S3")
    parser.add_argument("--log-dir", required=True, help="EFS log directory (contains slug subdirs)")
    parser.add_argument("--cluster-id", required=True, help="Cluster identifier for wants file key")
    parser.add_argument("--bucket", required=True, help="S3 bucket name")
    parser.add_argument("--interval", type=int, default=120, help="Sleep interval between cycles in seconds")
    parser.add_argument("--target-python", required=True, help="Comma-separated Python versions (e.g. 3.10,3.11)")
    parser.add_argument("--target-arch", required=True, help="Comma-separated architectures (e.g. x86_64,aarch64)")
    parser.add_argument("--target-manylinux", required=True, help="Target manylinux version (e.g. 2_17)")
    parser.add_argument("--once", action="store_true", help="Run a single iteration and exit")
    parser.add_argument(
        "--max-log-age-days", type=int, default=30, help="Delete fallback logs older than this many days"
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace, s3_client, http_get=None) -> None:
    """Main loop: scan logs, filter against PyPI, upload wants + cache."""
    if http_get is None:
        http_get = _default_http_get

    log_dir = Path(args.log_dir)
    python_versions = args.target_python.split(",")
    architectures = args.target_arch.split(",")
    matrix_header, matrix_combos = build_matrix(python_versions, architectures, args.target_manylinux)

    shutdown = False

    def handle_sigterm(_signum, _frame):
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGTERM, handle_sigterm)

    print(f"Wants collector starting: cluster={args.cluster_id}, matrix=[{matrix_header}]")

    while not shutdown:
        packages = scan_logs(log_dir)
        if not packages:
            print("No packages found in logs")
            Path("/tmp/last-success").touch()
            if args.once:
                return
            time.sleep(args.interval)
            continue

        print(f"Found {len(packages)} unique package+version pairs in logs")

        cache_content = download_from_s3(args.bucket, "prebuilt-cache.txt", s3_client)
        prebuilt = parse_prebuilt_cache(cache_content, matrix_header)

        needbuild_content = download_from_s3(args.bucket, "needbuild.txt", s3_client)
        needbuild = parse_needbuild(needbuild_content)
        if needbuild:
            print(f"Loaded {len(needbuild)} needbuild entries")

        try:
            wants, updated_prebuilt = filter_packages(
                packages, matrix_combos, args.target_manylinux, prebuilt, http_get, needbuild=needbuild
            )
        except Exception as e:
            print(f"WARNING: PyPI check failed ({e}), skipping cycle")
            if args.once:
                return
            time.sleep(args.interval)
            continue

        wants_key = f"wants/{args.cluster_id}.txt"
        upload_to_s3(format_wants(wants), args.bucket, wants_key, s3_client)
        upload_to_s3(
            format_prebuilt_cache(matrix_header, updated_prebuilt), args.bucket, "prebuilt-cache.txt", s3_client
        )
        print(f"Uploaded {len(wants)} wants entries, {len(updated_prebuilt)} prebuilt cache entries")

        Path("/tmp/last-success").touch()
        cleanup_old_logs(log_dir, args.max_log_age_days)
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
