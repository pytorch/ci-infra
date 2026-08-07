"""Smoke tests for the agent-sandbox module (no-proxy model).

Validates the deployed sandbox: namespace, the gvisor RuntimeClass, the
sandbox-agent worker + Service (callable from arc-runners), the credential model
(agent SA holds ONLY a read-only Bedrock IRSA role, no K8s token, no K8s RBAC),
and the ingress-only NetworkPolicy (egress is open in the prototype).
"""

from __future__ import annotations

import subprocess

import pytest
from helpers import assert_deployment_ready, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "ai-sandbox"
IRSA_KEY = "eks.amazonaws.com/role-arn"


class TestAgentSandboxNamespace:
    def test_namespace_exists(self, all_namespaces: dict) -> None:
        names = [ns["metadata"]["name"] for ns in all_namespaces.get("items", [])]
        assert NAMESPACE in names, f"Namespace '{NAMESPACE}' not found — agent-sandbox not deployed."


class TestAgentSandboxRuntimeClass:
    """The gvisor RuntimeClass is the isolation boundary for agent pods."""

    def test_gvisor_runtimeclass_exists(self) -> None:
        rc = run_kubectl(["get", "runtimeclass", "gvisor"])
        assert rc["handler"] == "runsc", f"gvisor RuntimeClass must use handler 'runsc', got {rc.get('handler')!r}."

    def test_gvisor_pins_sandbox_fleet(self) -> None:
        rc = run_kubectl(["get", "runtimeclass", "gvisor"])
        node_selector = rc.get("scheduling", {}).get("nodeSelector", {})
        assert node_selector.get("node-fleet") == "ai-sandbox", (
            f"gvisor RuntimeClass must pin node-fleet=ai-sandbox, got {node_selector!r}."
        )


class TestSandboxWorker:
    """The callable worker — a Deployment + Service reachable from arc-runners, like buildkitd."""

    def test_worker_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, "sandbox-agent")

    def test_worker_service_exists(self, all_services: dict) -> None:
        svcs = filter_services(all_services, namespace=NAMESPACE, name="sandbox-agent")
        assert len(svcs) == 1, f"Expected Service 'sandbox-agent' in '{NAMESPACE}'."

    def test_worker_runs_under_gvisor(self, all_deployments: dict) -> None:
        dep = next(d for d in all_deployments["items"] if d["metadata"]["name"] == "sandbox-agent")
        rc = dep["spec"]["template"]["spec"].get("runtimeClassName")
        assert rc == "gvisor", f"sandbox-agent must set runtimeClassName: gvisor, got {rc!r}."

    def test_callable_from_arc_runners(self) -> None:
        """A NetworkPolicy must allow arc-runners to reach the worker (buildkitd
        parity: network access, not RBAC, is the entry point)."""
        np = run_kubectl(["get", "networkpolicy", "sandbox-agent-ingress"], namespace=NAMESPACE)
        froms = [f for rule in np["spec"].get("ingress", []) for f in rule.get("from", [])]
        allowed_ns = {
            f.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") for f in froms
        }
        assert "arc-runners" in allowed_ns, (
            f"sandbox-agent-ingress must allow the arc-runners namespace; got {allowed_ns}."
        )


class TestAgentSandboxCredentials:
    def test_agent_sa_holds_only_bedrock_irsa(self) -> None:
        """The agent SA is bound to exactly one credential — a read-only Bedrock
        IRSA role — and does NOT automount a K8s token."""
        sa = run_kubectl(["get", "serviceaccount", "sandbox-agent"], namespace=NAMESPACE)
        assert sa.get("automountServiceAccountToken") is False, (
            "sandbox-agent SA must set automountServiceAccountToken: false."
        )
        ann = sa.get("metadata", {}).get("annotations", {})
        assert IRSA_KEY in ann, (
            "sandbox-agent SA must carry the Bedrock IRSA annotation — deploy.sh did not annotate it."
        )
        assert ann[IRSA_KEY].startswith("arn:aws:iam::"), f"IRSA annotation is not an IAM role ARN: {ann[IRSA_KEY]}"

    def test_agent_sa_has_no_rbac(self) -> None:
        """The agent SA must not be able to touch the K8s API at all."""
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-agent"
        for verb, resource in (("create", "pods"), ("get", "secrets"), ("list", "pods")):
            # `auth can-i` exits non-zero for "no"; run it directly (run_kubectl uses check=True).
            proc = subprocess.run(
                ["kubectl", "-n", NAMESPACE, "auth", "can-i", verb, resource, f"--as={subject}"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.stdout.strip() == "no", (
                f"sandbox-agent SA must NOT be able to {verb} {resource}; got {proc.stdout.strip()!r}."
            )


class TestAgentSandboxNetworkPolicies:
    def test_ingress_restricted_egress_open(self) -> None:
        """Prototype: ingress locked to arc-runners; egress is OPEN (no egress policy)."""
        nps = run_kubectl(["get", "networkpolicies"], namespace=NAMESPACE)
        items = nps.get("items", [])
        names = {np["metadata"]["name"] for np in items}
        assert {"default-deny-ingress", "sandbox-agent-ingress"} <= names, (
            f"expected ingress policies default-deny-ingress + sandbox-agent-ingress; got {sorted(names)}."
        )
        egress_policies = [np["metadata"]["name"] for np in items if "Egress" in np["spec"].get("policyTypes", [])]
        assert not egress_policies, (
            f"prototype egress must be OPEN — no Egress NetworkPolicy expected, found {egress_policies}."
        )
