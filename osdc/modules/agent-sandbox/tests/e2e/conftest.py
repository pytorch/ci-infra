"""Session fixtures for agent-sandbox e2e tests.

Run via: just test-agent-sandbox <cluster>  (kubeconfig is set by the recipe).
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--cluster-id", action="store", required=True, help="Cluster id from clusters.yaml")


@pytest.fixture(scope="session")
def cluster_id(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("--cluster-id")
