"""Smoke tests for the bin-pack-scheduler secondary scheduler.

Validates that the scheduler Deployment is ready and has acquired its
leader-election Lease (so it is actually scheduling, not just running).
"""

from __future__ import annotations

import pytest
from helpers import assert_deployment_ready, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "kube-system"
NAME = "bin-pack-scheduler"


class TestBinPackScheduler:
    def test_deployment_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, NAME)

    def test_leader_lease_held(self) -> None:
        """Leader election Lease exists and has a current holder."""
        lease = run_kubectl(["get", "lease", NAME], namespace=NAMESPACE)
        holder = lease.get("spec", {}).get("holderIdentity")
        assert holder, f"Lease {NAME} has no holderIdentity — no scheduler is leading"
