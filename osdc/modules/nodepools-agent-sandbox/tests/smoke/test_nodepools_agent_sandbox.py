"""Smoke tests for the nodepools-agent-sandbox module.

Validates the dedicated gVisor fleet: the NodePool exists with the isolating
node-fleet=ai-sandbox taint, and its EC2NodeClass userData carries the runsc
install script (without which runtimeClassName=gvisor pods can never start).
"""

from __future__ import annotations

import pytest
from helpers import run_kubectl

pytestmark = [pytest.mark.live]

# The generator names each NodePool "<fleet>-<instance size>", so resolve it from
# the fleet prefix rather than pinning a size — otherwise resizing the fleet in
# defs/ai-sandbox.yaml silently breaks every test in this file.
NODEPOOL_PREFIX = "ai-sandbox-"
MODULE_LABEL = "nodepools-agent-sandbox"


@pytest.fixture
def nodepool_name(all_nodepools: dict) -> str:
    all_names = sorted(np["metadata"]["name"] for np in all_nodepools.get("items", []))
    names = [n for n in all_names if n.startswith(NODEPOOL_PREFIX)]
    assert names, (
        f"No NodePool named {NODEPOOL_PREFIX}* found — nodepools-agent-sandbox not deployed. Existing: {all_names}"
    )
    assert len(names) == 1, (
        f"Expected exactly one {NODEPOOL_PREFIX}* NodePool (a stale one left behind by a fleet resize?), got {names}."
    )
    return names[0]


@pytest.fixture
def nodepool(all_nodepools: dict, nodepool_name: str) -> dict:
    return next(n for n in all_nodepools["items"] if n["metadata"]["name"] == nodepool_name)


class TestAgentSandboxNodePool:
    def test_nodepool_exists(self, nodepool_name: str) -> None:
        assert nodepool_name.startswith(NODEPOOL_PREFIX)

    def test_nodepool_has_fleet_taint(self, nodepool: dict) -> None:
        taints = nodepool["spec"]["template"]["spec"].get("taints", [])
        fleet_taints = [(t["key"], t.get("value")) for t in taints if t["key"] == "node-fleet"]
        assert ("node-fleet", "ai-sandbox") in fleet_taints, (
            f"NodePool '{nodepool['metadata']['name']}' must carry the node-fleet=ai-sandbox "
            f"NoSchedule taint; got {taints}."
        )

    def test_module_label(self, nodepool: dict) -> None:
        labels = nodepool["metadata"].get("labels", {})
        assert labels.get("osdc.io/module") == MODULE_LABEL, (
            f"NodePool '{nodepool['metadata']['name']}' must be labeled osdc.io/module={MODULE_LABEL}; got {labels}."
        )


class TestAgentSandboxNodeClass:
    def test_userdata_installs_gvisor(self, nodepool_name: str) -> None:
        """The EC2NodeClass userData must embed the runsc install so nodes register
        the gvisor containerd runtime handler at boot."""
        ec2nc = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws", nodepool_name])
        user_data = ec2nc["spec"].get("userData", "")
        assert "runsc" in user_data, f"EC2NodeClass '{nodepool_name}' userData must install runsc (gVisor)."
        assert "containerd.runtimes.runsc" in user_data, (
            f"EC2NodeClass '{nodepool_name}' userData must register the runsc containerd runtime handler."
        )
