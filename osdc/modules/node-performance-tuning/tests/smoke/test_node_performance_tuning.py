"""Smoke tests for node performance tuning DaemonSet."""

import pytest
from helpers import assert_daemonset_healthy

pytestmark = [pytest.mark.live]


def test_node_performance_tuning(all_daemonsets, all_nodes):
    assert_daemonset_healthy(all_daemonsets, all_nodes, "kube-system", "node-performance-tuning", allow_zero=True)
