#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Set or query the maxRunners capacity ConfigMap for a runner scale set.

Used during Phase 1 PoC testing of X-ScaleSetMaxCapacity. The forked
ghalistener watches this ConfigMap and calls listener.SetMaxRunners()
on change, which flows into the X-ScaleSetMaxCapacity header on the
next GitHub Actions Service poll.

ConfigMap convention:
    Name:      capacity-config-{normalized_runner_name}
    Namespace: arc-systems
    Key:       maxRunners

Usage:
    uv run scripts/python/capacity_setter.py set  --cluster-id arc-staging --runner-name l-x86iavx512-8-16 --value 5
    uv run scripts/python/capacity_setter.py get  --cluster-id arc-staging --runner-name l-x86iavx512-8-16
    uv run scripts/python/capacity_setter.py watch --cluster-id arc-staging --runner-name l-x86iavx512-8-16
    uv run scripts/python/capacity_setter.py delete --cluster-id arc-staging --runner-name l-x86iavx512-8-16
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

log = logging.getLogger("osdc-capacity")

NAMESPACE = "arc-systems"
CONFIGMAP_PREFIX = "capacity-config-"
CONFIGMAP_KEY = "maxRunners"
RUNNER_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def normalize_name(name: str) -> str:
    return name.replace(".", "-").replace("_", "-")


def configmap_name(runner_name: str) -> str:
    return f"{CONFIGMAP_PREFIX}{normalize_name(runner_name)}"


def load_clusters_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def kubectl_env() -> dict[str, str]:
    env = os.environ.copy()
    no_proxy = env.get("NO_PROXY", "")
    env["NO_PROXY"] = f"{no_proxy},.eks.amazonaws.com"
    env["no_proxy"] = f"{env.get('no_proxy', '')},.eks.amazonaws.com"
    return env


def validate_runner_name(name: str) -> None:
    normalized = normalize_name(name)
    if not normalized or not RUNNER_NAME_RE.match(normalized):
        log.error("Invalid runner name '%s' (normalized: '%s'). Must be DNS-1123 compliant.", name, normalized)
        sys.exit(1)


def run_kubectl(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    input_data: str | None = None,
    context: str | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["kubectl"]
    if context:
        cmd.extend(["--context", context])
    cmd.extend(args)
    log.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, input=input_data, env=kubectl_env())


def ensure_kubeconfig(cluster_name: str, region: str) -> None:
    env = kubectl_env()
    subprocess.run(
        [
            "aws",
            "eks",
            "update-kubeconfig",
            "--name",
            cluster_name,
            "--region",
            region,
            "--alias",
            cluster_name,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    log.info("Kubeconfig updated for %s", cluster_name)


def cmd_set(args: argparse.Namespace) -> None:
    cm_name = configmap_name(args.runner_name)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": cm_name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/component": "capacity-config",
                "osdc.io/module": "arc-runners",
                "osdc.io/runner-name": normalize_name(args.runner_name),
            },
        },
        "data": {CONFIGMAP_KEY: str(args.value)},
    }
    run_kubectl(
        ["apply", "-f", "-"],
        input_data=json.dumps(manifest),
        context=args.context,
    )
    log.info("Set %s: %s=%d", cm_name, CONFIGMAP_KEY, args.value)


def cmd_get(args: argparse.Namespace) -> None:
    cm_name = configmap_name(args.runner_name)
    result = run_kubectl(
        ["get", "configmap", cm_name, "-n", NAMESPACE, "-o", f"jsonpath={{.data.{CONFIGMAP_KEY}}}"],
        check=False,
        context=args.context,
    )
    if result.returncode != 0:
        log.error("ConfigMap %s not found in namespace %s", cm_name, NAMESPACE)
        sys.exit(1)

    value = result.stdout.strip()
    if not value:
        log.error("Key '%s' not found in ConfigMap %s", CONFIGMAP_KEY, cm_name)
        sys.exit(1)

    print(value)


def cmd_watch(args: argparse.Namespace) -> None:
    cm_name = configmap_name(args.runner_name)
    log.info("Watching %s/%s (Ctrl+C to stop)...", cm_name, CONFIGMAP_KEY)

    cmd = ["kubectl"]
    if args.context:
        cmd.extend(["--context", args.context])
    cmd.extend(["get", "configmap", cm_name, "-n", NAMESPACE, "-w"])
    try:
        subprocess.run(cmd, check=True, env=kubectl_env())
    except KeyboardInterrupt:
        print()
        log.info("Watch stopped.")
    except subprocess.CalledProcessError:
        log.error("ConfigMap %s not found or watch failed", cm_name)
        sys.exit(1)


def cmd_delete(args: argparse.Namespace) -> None:
    cm_name = configmap_name(args.runner_name)
    result = run_kubectl(
        ["delete", "configmap", cm_name, "-n", NAMESPACE],
        check=False,
        context=args.context,
    )
    if result.returncode == 0:
        log.info("Deleted %s", cm_name)
    else:
        log.warning("ConfigMap %s not found (already deleted?)", cm_name)


def cmd_list(args: argparse.Namespace) -> None:
    result = run_kubectl(
        [
            "get",
            "configmap",
            "-n",
            NAMESPACE,
            "-l",
            "app.kubernetes.io/component=capacity-config",
            "-o",
            "custom-columns=RUNNER:.metadata.labels.osdc\\.io/runner-name,MAX_RUNNERS:.data.maxRunners",
        ],
        context=args.context,
    )
    print(result.stdout.rstrip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set or query the maxRunners capacity ConfigMap for ARC runner scale sets.",
    )
    parser.add_argument("--cluster-id", required=True, help="Cluster ID from clusters.yaml")
    parser.add_argument(
        "--clusters-yaml",
        type=Path,
        default=Path(os.environ.get("CLUSTERS_YAML", Path(__file__).resolve().parent.parent.parent / "clusters.yaml")),
        help="Path to clusters.yaml",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Set maxRunners (creates ConfigMap if needed)")
    p_set.add_argument("--runner-name", required=True, help="Runner name from defs/ (e.g. l-x86iavx512-8-16)")
    p_set.add_argument("--value", type=int, required=True, help="maxRunners value (>= 0)")

    p_get = sub.add_parser("get", help="Get current maxRunners value")
    p_get.add_argument("--runner-name", required=True, help="Runner name from defs/")

    p_watch = sub.add_parser("watch", help="Watch maxRunners for changes (live)")
    p_watch.add_argument("--runner-name", required=True, help="Runner name from defs/")

    p_delete = sub.add_parser("delete", help="Delete the capacity ConfigMap")
    p_delete.add_argument("--runner-name", required=True, help="Runner name from defs/")

    sub.add_parser("list", help="List all capacity ConfigMaps in the cluster")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_clusters_yaml(args.clusters_yaml)
    clusters = cfg.get("clusters", {})
    if args.cluster_id not in clusters:
        known = ", ".join(clusters.keys())
        log.error("Unknown cluster '%s'. Known: %s", args.cluster_id, known)
        sys.exit(1)

    cluster_cfg = clusters[args.cluster_id]
    cluster_name = cluster_cfg["cluster_name"]
    region = cluster_cfg["region"]

    ensure_kubeconfig(cluster_name, region)
    args.context = cluster_name

    if hasattr(args, "runner_name") and args.runner_name:
        validate_runner_name(args.runner_name)

    if args.command == "set":
        if args.value < 0:
            log.error("--value must be >= 0, got %d", args.value)
            sys.exit(1)
        cmd_set(args)
    elif args.command == "get":
        cmd_get(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
