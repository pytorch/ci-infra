from __future__ import annotations

import pytest
from smoke_conftest import *  # noqa: F403

NAMESPACE = "hf-cache"


@pytest.fixture(scope="session")
def hf_cache_bucket(cluster_id) -> str:
    """Per-cluster model-cache bucket name (matches terraform/main.tf)."""
    return f"pytorch-hf-model-cache-{cluster_id}"
