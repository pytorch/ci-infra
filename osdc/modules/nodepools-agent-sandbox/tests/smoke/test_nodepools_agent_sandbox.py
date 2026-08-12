"""Smoke tests for the nodepools-agent-sandbox module.

Validates the dedicated gVisor fleet: the NodePool exists with the isolating
node-fleet=ai-sandbox taint, and its EC2NodeClass launches nodes from the custom
AMI with runsc baked in (without which runtimeClassName=gvisor pods can never
start) rather than installing it from userData at boot.
"""

from __future__ import annotations

import subprocess

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
    def test_selects_the_gvisor_ami(self, nodepool_name: str) -> None:
        """Nodes must come up from the custom AMI with runsc baked in — a stock
        AL2023 node has no runsc, so every runtimeClassName=gvisor pod would fail
        to start on it."""
        ec2nc = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws", nodepool_name])
        terms = ec2nc["spec"].get("amiSelectorTerms", [])
        tags = [t.get("tags", {}) for t in terms]
        assert any(t.get("osdc.io/ami") == "ai-sandbox-gvisor" for t in tags), (
            f"EC2NodeClass '{nodepool_name}' must select the gVisor AMI by tag "
            f"osdc.io/ami=ai-sandbox-gvisor; got amiSelectorTerms={terms}."
        )
        assert not any(t.get("alias", "").startswith("al2023") for t in terms), (
            f"EC2NodeClass '{nodepool_name}' must not fall back to the stock AL2023 alias; got {terms}."
        )

    def test_no_userdata_script(self, nodepool_name: str) -> None:
        """gVisor is baked into the AMI. A userData install would mean fetching
        binaries on every scale-up and restarting containerd underneath a running
        kubelet — see packer/scripts/install-gvisor.sh."""
        ec2nc = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws", nodepool_name])
        user_data = ec2nc["spec"].get("userData", "")
        assert "runsc" not in user_data, (
            f"EC2NodeClass '{nodepool_name}' userData must not install runsc — it belongs in the AMI."
        )
        assert "systemctl restart containerd" not in user_data, (
            f"EC2NodeClass '{nodepool_name}' userData must never restart containerd."
        )

    def test_ami_exists_in_region(self, nodepool_name: str, resolve_config) -> None:
        """The tag selector matches nothing until the AMI is built, and Karpenter
        then silently never provisions. Fail here with the fix instead.

        AMIs are regional, so this must query the cluster's region explicitly —
        mise pins AWS_REGION to the state-bucket region for the whole project.
        """
        region = resolve_config("region")
        proc = subprocess.run(
            [
                "aws",
                "ec2",
                "describe-images",
                "--region",
                region,
                "--owners",
                "self",
                "--filters",
                "Name=tag:osdc.io/ami,Values=ai-sandbox-gvisor",
                "Name=state,Values=available",
                "--query",
                "Images[].ImageId",
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"aws ec2 describe-images failed: {proc.stderr.strip()}"
        assert proc.stdout.strip(), (
            f"No AMI tagged osdc.io/ami=ai-sandbox-gvisor in {region} — the ai-sandbox fleet "
            f"cannot launch a node there. Build it with: just build-agent-sandbox-ami <cluster>"
        )
