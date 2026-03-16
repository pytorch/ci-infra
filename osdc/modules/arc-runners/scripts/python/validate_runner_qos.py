#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Validate ARC runner configs for Guaranteed QoS.

Checks that all generated job pod hook templates have:
    - resources.requests == resources.limits for CPU, memory, and GPU
    - Integer CPU values (not millicores)
    - Even CPU counts (warning only — topology manager prefers even)

Operates on: modules/arc-runners/generated/*.yaml
Called by: modules/arc-runners/deploy.sh before deploying
"""

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

# ANSI colors
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
NC = "\033[0m"


def extract_job_resources(configmap_yaml: str) -> dict:
    """Parse the ConfigMap YAML and extract CPU/memory/GPU limits and requests
    from the $job container.

    Returns a dict with keys: cpu_limit, cpu_request, mem_limit, mem_request,
    gpu_limit, gpu_request. Missing values are empty strings.
    """
    result = {
        "cpu_limit": "",
        "cpu_request": "",
        "mem_limit": "",
        "mem_request": "",
        "gpu_limit": "",
        "gpu_request": "",
    }

    # The ConfigMap embeds a YAML string in data.job-pod.yaml.
    # Parse the ConfigMap first, then parse the embedded YAML.
    try:
        cm = yaml.safe_load(configmap_yaml)
    except yaml.YAMLError:
        return result

    if not isinstance(cm, dict):
        return result

    data = cm.get("data", {})
    if not isinstance(data, dict):
        return result

    job_pod_str = data.get("job-pod.yaml", "")
    if not job_pod_str:
        return result

    try:
        job_pod = yaml.safe_load(job_pod_str)
    except yaml.YAMLError:
        return result

    if not isinstance(job_pod, dict):
        return result

    containers = job_pod.get("spec", {}).get("containers", [])
    if not isinstance(containers, list):
        return result

    # Find the $job container
    job_container = None
    for c in containers:
        if isinstance(c, dict) and c.get("name") == "$job":
            job_container = c
            break

    if job_container is None:
        return result

    resources = job_container.get("resources", {})
    if not isinstance(resources, dict):
        return result

    limits = resources.get("limits", {})
    requests = resources.get("requests", {})
    if not isinstance(limits, dict):
        limits = {}
    if not isinstance(requests, dict):
        requests = {}

    result["cpu_limit"] = str(limits.get("cpu", ""))
    result["cpu_request"] = str(requests.get("cpu", ""))
    result["mem_limit"] = str(limits.get("memory", ""))
    result["mem_request"] = str(requests.get("memory", ""))
    result["gpu_limit"] = str(limits.get("nvidia.com/gpu", ""))
    result["gpu_request"] = str(requests.get("nvidia.com/gpu", ""))

    return result


def validate_cpu_qos(cpu_limit: str, cpu_request: str) -> list[tuple[str, str]]:
    """Validate CPU resources for Guaranteed QoS.

    Returns list of (level, message) tuples where level is 'error'.
    Checks: both present, equal, integer (not millicores).
    """
    errors = []

    if not cpu_limit or not cpu_request:
        errors.append(("error", "Missing CPU limits or requests"))
        return errors

    if cpu_limit != cpu_request:
        errors.append(
            (
                "error",
                f"CPU mismatch: limits={cpu_limit}, requests={cpu_request} (must be equal for Guaranteed QoS)",
            )
        )
        return errors

    if not re.match(r"^[0-9]+$", cpu_limit):
        errors.append(
            (
                "error",
                f'CPU must be integer: {cpu_limit} (e.g., "4" not "4000m")',
            )
        )

    return errors


def validate_memory_qos(mem_limit: str, mem_request: str) -> list[tuple[str, str]]:
    """Validate memory resources for Guaranteed QoS.

    Returns list of (level, message) tuples where level is 'error'.
    Checks: both present, equal.
    """
    errors = []

    if not mem_limit or not mem_request:
        errors.append(("error", "Missing memory limits or requests"))
        return errors

    if mem_limit != mem_request:
        errors.append(
            (
                "error",
                f"Memory mismatch: limits={mem_limit}, requests={mem_request} (must be equal for Guaranteed QoS)",
            )
        )

    return errors


def validate_gpu_qos(gpu_limit: str, gpu_request: str) -> list[tuple[str, str]]:
    """Validate GPU resources for Guaranteed QoS (if GPU present).

    Returns list of (level, message) tuples where level is 'error'.
    Only validates if at least one of limit/request is set.
    """
    errors = []

    if not gpu_limit and not gpu_request:
        return errors

    if gpu_limit != gpu_request:
        errors.append(
            (
                "error",
                f"GPU mismatch: limits={gpu_limit}, requests={gpu_request}",
            )
        )

    return errors


def check_odd_cpu(cpu_value: str) -> list[tuple[str, str]]:
    """Warn if CPU count is odd (topology manager prefers even).

    Returns list of (level, message) tuples where level is 'warning'.
    Only warns if the value is a valid integer.
    """
    warnings = []

    if re.match(r"^[0-9]+$", cpu_value) and int(cpu_value) % 2 != 0:
        warnings.append(
            (
                "warning",
                f"Odd CPU count ({cpu_value}). Topology manager works best with even counts.",
            )
        )

    return warnings


def validate_file(filepath: Path) -> tuple[int, int]:
    """Validate a single generated runner config file.

    The file is a multi-document YAML: doc 0 is Helm values, doc 1+ is the
    ConfigMap. We extract the ConfigMap (after the --- separator) and validate
    the $job container resources.

    Returns (error_count, warning_count).
    """
    filename = filepath.name
    print(f"→ Validating: {filename}")

    content = filepath.read_text()

    # Split on the first --- separator to get the ConfigMap portion
    # (same logic as the bash script: awk '/^---$/,0')
    parts = content.split("\n---\n", 1)
    if len(parts) < 2:
        # Try split with --- at start of line followed by newline
        parts = content.split("\n---", 1)

    if len(parts) < 2:
        print(f"  {RED}✗{NC} No ConfigMap found (missing --- separator)")
        print()
        return 1, 0

    configmap_yaml = parts[1]

    # If split left a leading newline, that's fine for YAML parsing
    resources = extract_job_resources(configmap_yaml)

    errors = 0
    warnings = 0

    # Validate CPU
    cpu_issues = validate_cpu_qos(resources["cpu_limit"], resources["cpu_request"])
    for level, msg in cpu_issues:
        if level == "error":
            print(f"  {RED}✗{NC} {msg}")
            errors += 1
    if not cpu_issues:
        print(f"  {GREEN}✓{NC} CPU: {resources['cpu_limit']} (Guaranteed QoS)")

    # Validate Memory
    mem_issues = validate_memory_qos(resources["mem_limit"], resources["mem_request"])
    for level, msg in mem_issues:
        if level == "error":
            print(f"  {RED}✗{NC} {msg}")
            errors += 1
    if not mem_issues:
        print(f"  {GREEN}✓{NC} Memory: {resources['mem_limit']} (Guaranteed QoS)")

    # Validate GPU
    gpu_issues = validate_gpu_qos(resources["gpu_limit"], resources["gpu_request"])
    for level, msg in gpu_issues:
        if level == "error":
            print(f"  {RED}✗{NC} {msg}")
            errors += 1
    if not gpu_issues and (resources["gpu_limit"] or resources["gpu_request"]):
        print(f"  {GREEN}✓{NC} GPU: {resources['gpu_limit']}")

    # Warn on odd CPU
    cpu_warnings = check_odd_cpu(resources["cpu_limit"])
    for level, msg in cpu_warnings:
        if level == "warning":
            print(f"  {YELLOW}⚠{NC}  {msg}")
            warnings += 1

    print()
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Validates all generated runner configs in a directory.

    Returns 0 on success, 1 on validation failure.
    """
    parser = argparse.ArgumentParser(description="Validate ARC runner configs for Guaranteed QoS")
    parser.add_argument(
        "directory",
        nargs="?",
        help="Directory containing generated runner YAML files (default: ARC_RUNNERS_OUTPUT_DIR or ../generated/)",
    )
    args = parser.parse_args(argv)

    # Resolve directory
    if args.directory:
        generated_dir = Path(args.directory)
    else:
        env_dir = os.environ.get("ARC_RUNNERS_OUTPUT_DIR")
        if env_dir:
            generated_dir = Path(env_dir)
        else:
            script_dir = Path(__file__).resolve().parent
            generated_dir = script_dir.parent.parent / "generated"

    print("━" * 61)
    print("Runner QoS Validation")
    print("━" * 61)
    print()

    yaml_files = sorted(generated_dir.glob("*.yaml"))
    if not yaml_files:
        print(f"{RED}No generated runner configs found in {generated_dir}{NC}")
        print("Run generate_runners.py first.")
        return 1

    total_errors = 0
    total_warnings = 0

    for config_path in yaml_files:
        file_errors, file_warnings = validate_file(config_path)
        total_errors += file_errors
        total_warnings += file_warnings

    print("━" * 61)
    print(f"Configs checked: {len(yaml_files)} | Errors: {total_errors} | Warnings: {total_warnings}")
    print("━" * 61)

    if total_errors > 0:
        print(f"{RED}Validation FAILED{NC}")
        return 1

    print(f"{GREEN}All runners have Guaranteed QoS configuration.{NC}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
