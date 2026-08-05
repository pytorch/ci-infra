"""Smoke tests for the nodepools-agent-sandbox module.

Validates the dedicated gVisor fleet: the NodePool exists with the isolating
node-fleet=ai-sandbox taint, and its EC2NodeClass userData carries the runsc
install script (without which runtimeClassName=gvisor pods can never start).
"""

from __future__ import annotations

import pytest
from helpers import run_kubectl

pytestmark = [pytest.mark.live]

NODEPOOL = "ai-sandbox-8xlarge"
MODULE_LABEL = "nodepools-agent-sandbox"


class TestAgentSandboxNodePool:
    def test_nodepool_exists(self, all_nodepools: dict) -> None:
        names = {np["metadata"]["name"] for np in all_nodepools.get("items", [])}
        assert NODEPOOL in names, f"NodePool '{NODEPOOL}' not found. Existing: {sorted(names)}"

    def test_nodepool_has_fleet_taint(self, all_nodepools: dict) -> None:
        np = next(n for n in all_nodepools["items"] if n["metadata"]["name"] == NODEPOOL)
        taints = np["spec"]["template"]["spec"].get("taints", [])
        fleet_taints = [(t["key"], t.get("value")) for t in taints if t["key"] == "node-fleet"]
        assert ("node-fleet", "ai-sandbox") in fleet_taints, (
            f"NodePool '{NODEPOOL}' must carry the node-fleet=ai-sandbox NoSchedule taint; got {taints}."
        )

    def test_module_label(self, all_nodepools: dict) -> None:
        np = next(n for n in all_nodepools["items"] if n["metadata"]["name"] == NODEPOOL)
        labels = np["metadata"].get("labels", {})
        assert labels.get("osdc.io/module") == MODULE_LABEL, (
            f"NodePool '{NODEPOOL}' must be labeled osdc.io/module={MODULE_LABEL}; got {labels}."
        )


class TestAgentSandboxNodeClass:
    def test_userdata_installs_gvisor(self) -> None:
        """The EC2NodeClass userData must embed the runsc install so nodes register
        the gvisor containerd runtime handler at boot."""
        ec2nc = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws", NODEPOOL])
        user_data = ec2nc["spec"].get("userData", "")
        assert "runsc" in user_data, f"EC2NodeClass '{NODEPOOL}' userData must install runsc (gVisor)."
        assert "containerd.runtimes.runsc" in user_data, (
            f"EC2NodeClass '{NODEPOOL}' userData must register the runsc containerd runtime handler."
        )
