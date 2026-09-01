"""Smoke tests for the nodepools-agent-sandbox module.

Validates the dedicated gVisor fleet: the NodePool exists with the isolating
node-fleet=ai-sandbox taint, and its EC2NodeClass launches nodes from the custom
AMI with runsc baked in (without which runtimeClassName=gvisor pods can never
start) rather than installing it from userData at boot.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from helpers import run_kubectl

pytestmark = [pytest.mark.live]

# The generator names each NodePool "<fleet>-<instance size>", so resolve it from
# the fleet prefix rather than pinning a size — otherwise resizing the fleet in
# defs/ai-sandbox.yaml silently breaks every test in this file.
#
# The GPU fleet is "ai-sandbox-gpu-*", which also starts with the CPU prefix, so the CPU
# lookup has to exclude it explicitly. Matching loosely here would make a GPU fleet look
# like a stale duplicate of the CPU one.
NODEPOOL_PREFIX = "ai-sandbox-"
GPU_NODEPOOL_PREFIX = "ai-sandbox-gpu-"
MODULE_LABEL = "nodepools-agent-sandbox"


def _one_nodepool(all_nodepools: dict, prefix: str, exclude_prefix: str | None = None) -> str:
    all_names = sorted(np["metadata"]["name"] for np in all_nodepools.get("items", []))
    names = [n for n in all_names if n.startswith(prefix)]
    if exclude_prefix:
        names = [n for n in names if not n.startswith(exclude_prefix)]
    assert names, f"No NodePool named {prefix}* found — nodepools-agent-sandbox not deployed. Existing: {all_names}"
    assert len(names) == 1, (
        f"Expected exactly one {prefix}* NodePool (a stale one left behind by a fleet resize?), got {names}."
    )
    return names[0]


@pytest.fixture
def nodepool_name(all_nodepools: dict) -> str:
    return _one_nodepool(all_nodepools, NODEPOOL_PREFIX, exclude_prefix=GPU_NODEPOOL_PREFIX)


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

        Also guards the Kubernetes version: the def selects on osdc.io/ami alone,
        so the K8sVersion the build records is never read back at launch and an EKS
        bump would keep launching pre-upgrade nodes until the AMI is rebuilt (see
        the TODO in defs/ai-sandbox.yaml).

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
                # Newest first: that is the one Karpenter launches, so it is the one
                # whose K8sVersion has to match.
                "--query",
                "reverse(sort_by(Images, &CreationDate))[].{id: ImageId, tags: Tags}",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"aws ec2 describe-images failed: {proc.stderr.strip()}"
        images = json.loads(proc.stdout or "[]")
        assert images, (
            f"No AMI tagged osdc.io/ami=ai-sandbox-gvisor in {region} — the ai-sandbox fleet "
            f"cannot launch a node there. Build it with: just build-agent-sandbox-ami <cluster>"
        )

        newest = images[0]
        built_for = {t["Key"]: t["Value"] for t in newest.get("tags") or []}.get("K8sVersion")
        expected = str(resolve_config("eks_version"))
        assert built_for == expected, (
            f"Newest ai-sandbox AMI {newest['id']} in {region} was built for k8s {built_for!r}, "
            f"but the cluster runs {expected!r} — the fleet selects by tag only and would launch "
            f"nodes on the stale version. Rebuild it with: just build-agent-sandbox-ami <cluster>"
        )


class TestAgentSandboxGpuNodePool:
    """The GPU half of the sandbox. Separate fleet on purpose: nvproxy forwards NVIDIA
    ioctls to the host driver, so a driver bug is a node takeover, and these nodes must
    never be shared with CI work. See docs/agent-sandbox-gpu-gvisor.md."""

    @pytest.fixture
    def gpu_nodepool(self, all_nodepools: dict) -> dict:
        name = _one_nodepool(all_nodepools, GPU_NODEPOOL_PREFIX)
        return next(n for n in all_nodepools["items"] if n["metadata"]["name"] == name)

    def test_isolating_taint(self, gpu_nodepool: dict) -> None:
        taints = gpu_nodepool["spec"]["template"]["spec"].get("taints", [])
        fleet_taints = [(t["key"], t.get("value")) for t in taints if t["key"] == "node-fleet"]
        assert ("node-fleet", "ai-sandbox-gpu") in fleet_taints, (
            f"the GPU fleet must carry node-fleet=ai-sandbox-gpu so CI work cannot land on it; got {taints}."
        )

    def test_one_gpu_per_node(self, gpu_nodepool: dict) -> None:
        """One GPU per node is what makes it one task per node — a task requests
        nvidia.com/gpu: 1, so a second cannot schedule regardless of CPU and memory. That
        is what removes cross-task VRAM residue and GPU side channels."""
        requirements = gpu_nodepool["spec"]["template"]["spec"].get("requirements", [])
        types = [r["values"] for r in requirements if r["key"] == "node.kubernetes.io/instance-type"]
        assert types, f"GPU fleet must pin its instance type; got {requirements}."
        assert all(t.endswith(".4xlarge") for t in types[0]), f"expected single-GPU g6.4xlarge nodes; got {types[0]}."

    def test_selects_the_nvproxy_ami(self, all_nodepools: dict) -> None:
        """A stock NVIDIA AMI has no runsc, so every gvisor-gpu pod would fail to start."""
        name = _one_nodepool(all_nodepools, GPU_NODEPOOL_PREFIX)
        ec2nc = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws", name])
        terms = ec2nc["spec"].get("amiSelectorTerms", [])
        tags = [t.get("tags", {}) for t in terms]
        assert any(t.get("osdc.io/ami") == "ai-sandbox-gpu-gvisor" for t in tags), (
            f"EC2NodeClass '{name}' must select the nvproxy AMI by tag; got {terms}."
        )

    def test_gpu_ami_exists_in_region(self, resolve_config) -> None:
        """Same tag-selector trap as the CPU fleet, with an extra failure mode: the AMI
        pins an NVIDIA driver that gVisor's nvproxy must support, or sandboxes cannot
        start even though the node comes up healthy."""
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
                "Name=tag:osdc.io/ami,Values=ai-sandbox-gpu-gvisor",
                "Name=state,Values=available",
                "--query",
                "reverse(sort_by(Images, &CreationDate))[].{id: ImageId, tags: Tags}",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"aws ec2 describe-images failed: {proc.stderr.strip()}"
        images = json.loads(proc.stdout or "[]")
        assert images, (
            f"No AMI tagged osdc.io/ami=ai-sandbox-gpu-gvisor in {region} — the GPU fleet cannot launch "
            f"a node there. Build it with: just build-agent-sandbox-gpu-ami <cluster>"
        )
        newest = images[0]
        tags = {t["Key"]: t["Value"] for t in newest.get("tags") or []}
        expected = str(resolve_config("eks_version"))
        assert tags.get("K8sVersion") == expected, (
            f"Newest GPU AMI {newest['id']} was built for k8s {tags.get('K8sVersion')!r}, cluster runs "
            f"{expected!r}. Rebuild with: just build-agent-sandbox-gpu-ami <cluster>"
        )
