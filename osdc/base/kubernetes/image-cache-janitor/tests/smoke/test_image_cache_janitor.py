"""Smoke tests for the image cache janitor DaemonSet."""

import pytest
from helpers import assert_daemonset_healthy

pytestmark = [pytest.mark.live]

NAMESPACE = "kube-system"


class TestImageCacheJanitor:
    """Verify the image cache janitor DaemonSet."""

    def test_daemonset_exists_and_ready(self, all_daemonsets, all_nodes):
        assert_daemonset_healthy(
            all_daemonsets,
            all_nodes,
            NAMESPACE,
            "image-cache-janitor",
            node_selector={"workload-type": ["github-runner", "buildkit"]},
            min_node_age=900,
        )
