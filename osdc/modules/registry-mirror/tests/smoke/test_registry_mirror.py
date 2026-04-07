"""Smoke tests for registry mirror DaemonSet."""

import pytest
from helpers import assert_daemonset_healthy

pytestmark = [pytest.mark.live]


def test_registry_mirror_config(all_daemonsets, all_nodes):
    assert_daemonset_healthy(all_daemonsets, all_nodes, "kube-system", "registry-mirror-config")
