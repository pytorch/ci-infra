"""Smoke tests for the kubelet's single-process OOM kill behaviour.

Three layers, cheapest first: the EC2NodeClass asks for it, the running kubelet
reports it, and a container that overruns its memory limit actually loses only
the offending process.
"""

from __future__ import annotations

import email
import json
import subprocess
import time
import uuid

import pytest
import yaml
from helpers import DEFAULT_TIMEOUT, _proxy_bypass_env, run_kubectl

pytestmark = [pytest.mark.live]

MODULE_LABEL = "osdc.io/module"
MODULE = "nodepools"
NODEPOOL_LABEL = "karpenter.sh/nodepool"
HASH_ANNOTATION = "karpenter.k8s.aws/ec2nodeclass-hash"
PROBE_NAMESPACE = "default"
PROBE_IMAGE = "public.ecr.aws/docker/library/alpine:3.21"
PROBE_LIMIT = "128Mi"
PROBE_TIMEOUT_S = 180

# sleep is the canary and tail /dev/zero is the balloon: tail keeps the whole
# unterminated stream in memory, so it is far and away the highest-badness
# process in the cgroup and the one the kernel should pick.
PROBE_SCRIPT = """\
sleep 600 &
canary=$!
echo "oom_score_adj=$(cat /proc/self/oom_score_adj) oom_group=$(cat /sys/fs/cgroup/memory.oom.group)"
tail /dev/zero
echo "balloon_reaped"
if kill -0 "$canary" 2>/dev/null; then echo CANARY_SURVIVED_OOM; else echo CANARY_DIED; fi
sleep 60
"""


def _kubelet_config(ec2_node_class: dict) -> dict:
    """Return spec.kubelet.config from the NodeConfig MIME part of userData."""
    message = email.message_from_string(ec2_node_class["spec"]["userData"])
    for part in message.walk():
        if part.get_content_type() != "application/node.eks.aws":
            continue
        doc = yaml.safe_load(part.get_payload(decode=False))
        if isinstance(doc, dict) and doc.get("kind") == "NodeConfig":
            return doc["spec"]["kubelet"]["config"]
    raise AssertionError(f"no NodeConfig MIME part in EC2NodeClass {ec2_node_class['metadata']['name']}")


@pytest.fixture(scope="module")
def node_classes() -> dict[str, dict]:
    """EC2NodeClasses owned by this module, keyed by name (== nodepool name)."""
    items = run_kubectl(["get", "ec2nodeclasses.karpenter.k8s.aws"])["items"]
    owned = {e["metadata"]["name"]: e for e in items if e["metadata"].get("labels", {}).get(MODULE_LABEL) == MODULE}
    assert owned, f"no EC2NodeClasses labelled {MODULE_LABEL}={MODULE}"
    return owned


@pytest.fixture(scope="module")
def current_nodes(node_classes: dict[str, dict], all_nodes: dict) -> list[dict]:
    """Nodes booted from the EC2NodeClass revision that is live right now.

    Karpenter stamps the nodeclass hash onto each node. After a userData change
    the old nodes keep serving until they drift out, and their kubelet still has
    the old config — asserting on them would fail every deploy until the fleet
    rolls, so they are excluded rather than reported as broken.
    """
    current = []
    for node in all_nodes.get("items", []):
        node_class = node_classes.get(node["metadata"].get("labels", {}).get(NODEPOOL_LABEL))
        if node_class is None:
            continue
        if not _is_ready(node):
            continue
        if node["metadata"].get("annotations", {}).get(HASH_ANNOTATION) == node_class["metadata"]["annotations"].get(
            HASH_ANNOTATION
        ):
            current.append(node)
    return current


def _is_ready(node: dict) -> bool:
    return any(c["type"] == "Ready" and c["status"] == "True" for c in node.get("status", {}).get("conditions", []))


class TestNodeClassRequestsIt:
    def test_every_node_class_enables_single_process_oom_kill(self, node_classes: dict[str, dict]) -> None:
        missing = sorted(
            n for n, e in node_classes.items() if _kubelet_config(e).get("singleProcessOOMKill") is not True
        )
        assert not missing, (
            f"{len(missing)}/{len(node_classes)} EC2NodeClasses without singleProcessOOMKill, first few: {missing[:5]}"
        )


class TestKubeletAppliedIt:
    def test_kubelets_report_single_process_oom_kill(self, current_nodes: list[dict]) -> None:
        """Ask one node per nodepool what config it actually booted with.

        The EC2NodeClass only proves what we asked for; configz is the kubelet's
        own answer, so a userData block that nodeadm parsed but ignored shows up
        here and nowhere else.
        """
        if not current_nodes:
            pytest.skip("no nodes on the current EC2NodeClass revision yet")

        sample = {n["metadata"]["labels"][NODEPOOL_LABEL]: n["metadata"]["name"] for n in current_nodes}
        wrong, answered = {}, 0
        for nodepool, node in sorted(sample.items()):
            try:
                raw = run_kubectl(["get", "--raw", f"/api/v1/nodes/{node}/proxy/configz"], json_output=False)
            except subprocess.CalledProcessError:
                # Scaled away between listing and probing — normal on this fleet.
                continue
            answered += 1
            value = json.loads(raw)["kubeletconfig"].get("singleProcessOOMKill")
            if value is not True:
                wrong[nodepool] = f"{node} reports {value!r}"
        if not answered:
            pytest.skip("no sampled node stayed up long enough to answer configz")
        assert not wrong, (
            f"{len(wrong)}/{answered} sampled kubelets not running with singleProcessOOMKill, "
            f"first few: {dict(sorted(wrong.items())[:5])}"
        )


class TestOOMKillsOnlyTheOffender:
    """The behaviour the flag exists for, exercised end to end on a real node."""

    def test_a_container_oom_spares_the_rest_of_the_container(self, current_nodes: list[dict]) -> None:
        if not current_nodes:
            pytest.skip("no nodes on the current EC2NodeClass revision yet")

        node = current_nodes[0]["metadata"]["name"]
        name = f"osdc-oom-probe-{uuid.uuid4().hex[:8]}"
        manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": {"osdc.io/smoke": "oom-kill"}},
            "spec": {
                "restartPolicy": "Never",
                "nodeName": node,
                "tolerations": [{"operator": "Exists"}],
                "containers": [
                    {
                        "name": "probe",
                        "image": PROBE_IMAGE,
                        "command": ["/bin/sh", "-c", PROBE_SCRIPT],
                        # Guaranteed, to get the same oom_score_adj (-997) the
                        # kubelet gives the runner pods this is modelling.
                        "resources": {
                            "requests": {"cpu": "100m", "memory": PROBE_LIMIT},
                            "limits": {"cpu": "100m", "memory": PROBE_LIMIT},
                        },
                    }
                ],
            },
        }

        try:
            subprocess.run(
                ["kubectl", "-n", PROBE_NAMESPACE, "apply", "--request-timeout=60s", "-f", "-"],
                input=json.dumps(manifest),
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT,
                check=True,
                env=_proxy_bypass_env(),
            )
            logs, status = self._await_verdict(name)
        finally:
            subprocess.run(
                ["kubectl", "-n", PROBE_NAMESPACE, "delete", "pod", name, "--wait=false", "--ignore-not-found"],
                capture_output=True,
                timeout=DEFAULT_TIMEOUT,
                env=_proxy_bypass_env(),
            )

        assert "CANARY_SURVIVED_OOM" in logs, (
            f"the balloon took the whole container down with it on {node}.\n"
            f"container state: {status}\nlogs: {logs!r}\n"
            "A group kill is why an OOMed workflow job reports a bare exit 137 "
            "with no output: the shell that would have said which step died is "
            "killed in the same sweep."
        )
        assert "terminated" not in status, f"probe container exited instead of surviving: {status}"

    def _await_verdict(self, name: str) -> tuple[str, dict]:
        """Poll until the probe prints a verdict or its container dies."""
        deadline = time.monotonic() + PROBE_TIMEOUT_S
        logs, status = "", {}
        while time.monotonic() < deadline:
            time.sleep(5)
            pod = run_kubectl(["get", "pod", name], namespace=PROBE_NAMESPACE)
            statuses = pod.get("status", {}).get("containerStatuses") or []
            status = statuses[0]["state"] if statuses else {}
            try:
                logs = run_kubectl(["logs", name], namespace=PROBE_NAMESPACE, json_output=False)
            except subprocess.CalledProcessError:
                continue
            if "CANARY_SURVIVED_OOM" in logs or "CANARY_DIED" in logs or "terminated" in status:
                break
        return logs, status
