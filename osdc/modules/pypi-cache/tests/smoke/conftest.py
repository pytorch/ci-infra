from __future__ import annotations

import pytest
from helpers import filter_pods
from smoke_conftest import *  # noqa: F403

NAMESPACE = "pypi-cache"


def _cuda_slug(version: str) -> str:
    """Normalize CUDA version to slug (major.minor only, e.g. 12.8.1 -> cu128)."""
    parts = version.split(".")
    return f"cu{parts[0]}{parts[1]}"


@pytest.fixture(scope="session")
def pypi_cache_slugs(resolve_config) -> list[str]:
    """Compute CUDA slugs from cluster config. Always includes 'cpu'."""
    cuda_versions = resolve_config("pypi_cache.cuda_versions", [])
    return ["cpu"] + [_cuda_slug(v) for v in cuda_versions]


@pytest.fixture(scope="session")
def pypi_cache_pods(all_pods, pypi_cache_slugs) -> dict[str, str]:
    """Map slug -> Running pod name for each pypi-cache deployment."""
    pods_by_slug: dict[str, str] = {}
    for slug in pypi_cache_slugs:
        matching = filter_pods(all_pods, NAMESPACE, labels={"app": "pypi-cache", "cuda-version": slug})
        running = [p for p in matching if p["status"]["phase"] == "Running"]
        if running:
            pods_by_slug[slug] = running[0]["metadata"]["name"]
    return pods_by_slug


@pytest.fixture(scope="session")
def wants_collector_pod(all_pods) -> str | None:
    """Pod name for a Running wants-collector pod, or None."""
    matching = filter_pods(all_pods, NAMESPACE, labels={"app": "pypi-wants-collector"})
    running = [p for p in matching if p["status"]["phase"] == "Running"]
    return running[0]["metadata"]["name"] if running else None
