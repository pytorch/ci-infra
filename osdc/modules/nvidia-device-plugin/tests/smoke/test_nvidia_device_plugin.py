"""Smoke tests for NVIDIA device plugin DaemonSet."""

import pytest
from helpers import assert_daemonset_healthy

pytestmark = [pytest.mark.live]


def test_nvidia_device_plugin(all_daemonsets, all_nodes):
    # 0/0 is OK if no GPU nodes are present
    assert_daemonset_healthy(
        all_daemonsets, all_nodes, "kube-system", "nvidia-device-plugin-daemonset", allow_zero=True
    )
