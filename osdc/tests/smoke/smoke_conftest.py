"""Shared fixtures for OSDC smoke tests.

This is a regular Python module (not a conftest.py) because pytest conftest
discovery walks from rootdir DOWN to each test file. Module-level test
directories (e.g. modules/arc/tests/smoke/) are not descendants of
tests/smoke/, so a conftest.py here would never be found.

Each test directory has a tiny conftest.py that does:
    from smoke_conftest import *  # noqa: F401, F403

The just recipe sets PYTHONPATH to include tests/smoke/ so the import works.

Smoke tests are designed to run concurrently with other cluster operations
(compactor e2e tests, Karpenter scaling, node recycling). DaemonSet checks
use assert_daemonset_healthy() which tolerates mismatches caused by nodes
in transition (new, NotReady, or being deleted). Deployment checks use a
90-second retry window for rollout tolerance.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
from helpers import run_helm, run_kubectl

__all__ = [
    "all_daemonsets",
    "all_deployments",
    "all_helm_releases",
    "all_namespaces",
    "all_nodepools",
    "all_nodes",
    "all_pods",
    "all_services",
    "all_storageclasses",
    "cluster_config",
    "cluster_id",
    "enabled_modules",
    "pytest_addoption",
    "resolve_config",
    "root_dir",
    "upstream_dir",
    "validate_kubeconfig",
]


# ---------------------------------------------------------------------------
# CLI option — try/except because the e2e conftest also registers --cluster-id
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    with contextlib.suppress(ValueError):
        parser.addoption(
            "--cluster-id",
            action="store",
            required=True,
            help="Cluster ID from clusters.yaml (e.g. arc-staging)",
        )


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cluster_id(request: pytest.FixtureRequest) -> str:
    """Cluster ID from --cluster-id CLI option."""
    return request.config.getoption("--cluster-id")


@pytest.fixture(scope="session")
def upstream_dir() -> Path:
    """Path to the upstream osdc/ directory (OSDC_UPSTREAM env var)."""
    val = os.environ.get("OSDC_UPSTREAM", "")
    if not val:
        pytest.fail("OSDC_UPSTREAM environment variable is not set")
    p = Path(val)
    if not p.is_dir():
        pytest.fail(f"OSDC_UPSTREAM does not exist: {p}")
    return p


@pytest.fixture(scope="session")
def root_dir() -> Path:
    """Path to the consumer osdc/ directory (OSDC_ROOT env var)."""
    val = os.environ.get("OSDC_ROOT", "")
    if not val:
        pytest.fail("OSDC_ROOT environment variable is not set")
    p = Path(val)
    if not p.is_dir():
        pytest.fail(f"OSDC_ROOT does not exist: {p}")
    return p


@pytest.fixture(scope="session")
def cluster_config(cluster_id: str, upstream_dir: Path) -> dict:
    """Full cluster config dict from clusters.yaml, resolved for the given cluster."""
    cfg_path = upstream_dir / "scripts" / "cluster-config.py"
    if not cfg_path.exists():
        pytest.fail(f"cluster-config.py not found at {cfg_path}")

    spec = importlib.util.spec_from_file_location("cluster_config_mod", cfg_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    full = mod.load_config()
    clusters = full.get("clusters", {})
    if cluster_id not in clusters:
        pytest.fail(f"Cluster '{cluster_id}' not found in clusters.yaml")

    return {
        "cluster": clusters[cluster_id],
        "defaults": full.get("defaults", {}),
        "cluster_id": cluster_id,
        "_module": mod,
    }


@pytest.fixture(scope="session")
def resolve_config(cluster_config: dict):
    """Return a callable that resolves a dot-path against cluster config with defaults.

    Usage in tests::

        def test_something(resolve_config):
            replicas = resolve_config("harbor.core_replicas")
    """
    mod = cluster_config["_module"]
    cluster_cfg = cluster_config["cluster"]
    defaults = cluster_config["defaults"]

    def _resolve(dotpath: str, default=None):
        val = mod.resolve(cluster_cfg, defaults, dotpath)
        if val is None:
            return default
        return val

    return _resolve


@pytest.fixture(scope="session")
def enabled_modules(cluster_config: dict) -> list[str]:
    """List of modules enabled for this cluster (from clusters.yaml)."""
    return cluster_config["cluster"].get("modules", [])


# ---------------------------------------------------------------------------
# Kubeconfig validation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def validate_kubeconfig(cluster_config: dict) -> None:
    """Verify the current kubectl context matches the target cluster."""
    expected_name = cluster_config["cluster"].get("cluster_name", "")
    if not expected_name:
        pytest.fail("cluster_name not set in cluster config")

    result = subprocess.run(
        ["kubectl", "config", "current-context"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"kubectl not configured: {result.stderr.strip()}")

    context = result.stdout.strip()
    if expected_name not in context:
        pytest.fail(
            f"kubectl context mismatch: current context is '{context}', expected it to contain '{expected_name}'"
        )


# ---------------------------------------------------------------------------
# Batch-fetch fixtures (session-scoped, ~10 kubectl/helm calls total)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def all_pods() -> dict:
    """All pods across all namespaces."""
    return run_kubectl(["get", "pods", "-A"])


@pytest.fixture(scope="session")
def all_deployments() -> dict:
    """All deployments across all namespaces."""
    return run_kubectl(["get", "deployments", "-A"])


@pytest.fixture(scope="session")
def all_daemonsets() -> dict:
    """All daemonsets across all namespaces."""
    return run_kubectl(["get", "daemonsets", "-A"])


@pytest.fixture(scope="session")
def all_services() -> dict:
    """All services across all namespaces."""
    return run_kubectl(["get", "services", "-A"])


@pytest.fixture(scope="session")
def all_namespaces() -> dict:
    """All namespaces."""
    return run_kubectl(["get", "namespaces"])


@pytest.fixture(scope="session")
def all_nodes() -> dict:
    """All nodes."""
    return run_kubectl(["get", "nodes"])


@pytest.fixture(scope="session")
def all_helm_releases() -> list[dict]:
    """All Helm releases across all namespaces."""
    try:
        return run_helm(["list", "-A"], timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []


@pytest.fixture(scope="session")
def all_nodepools() -> dict:
    """All Karpenter NodePools (returns empty items list if CRD not installed)."""
    try:
        return run_kubectl(["get", "nodepools.karpenter.sh"])
    except subprocess.CalledProcessError:
        return {"items": []}


@pytest.fixture(scope="session")
def all_storageclasses() -> dict:
    """All StorageClasses."""
    return run_kubectl(["get", "storageclasses"])
