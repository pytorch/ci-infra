"""Smoke tests for the agent-sandbox module.

Validates the deployed standing infrastructure: namespace, the gvisor
RuntimeClass, the two egress proxies (sigv4-proxy + sigv4-proxy), their Services,
the IRSA-annotated sigv4-proxy SA, the credential-free sandbox-agent SA, and the
NetworkPolicies. These check the security spine is wired — not that an agent run
succeeds (that is the e2e test).
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
        """scheduling.nodeSelector must pin agent pods to the ai-sandbox fleet so
        they can only land on nodes that actually have the runsc handler."""
        rc = run_kubectl(["get", "runtimeclass", "gvisor"])
        node_selector = rc.get("scheduling", {}).get("nodeSelector", {})
        assert node_selector.get("node-fleet") == "ai-sandbox", (
            f"gvisor RuntimeClass must pin node-fleet=ai-sandbox, got {node_selector!r}."
        )


class TestSigv4Proxy:
    """The proxy is what makes the sandbox credential-free: it holds the IRSA role
    and signs on the agent's behalf."""

    def test_sigv4_proxy_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, "sigv4-proxy")

    def test_proxy_service_exists(self, all_services: dict) -> None:
        svcs = filter_services(all_services, namespace=NAMESPACE, name="sigv4-proxy")
        assert len(svcs) == 1, f"Expected Service 'sigv4-proxy' in '{NAMESPACE}'."


class TestSandboxWorker:
    """The callable worker — a Deployment + Service reachable from arc-runners,
    like buildkitd."""

    def test_worker_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, "sandbox-agent")

    def test_worker_service_exists(self, all_services: dict) -> None:
        svcs = filter_services(all_services, namespace=NAMESPACE, name="sandbox-agent")
        assert len(svcs) == 1, f"Expected Service 'sandbox-agent' in '{NAMESPACE}'."

    def test_worker_runs_under_gvisor(self, all_deployments: dict) -> None:
        """The worker Deployment must request the gvisor RuntimeClass."""
        dep = next(d for d in all_deployments["items"] if d["metadata"]["name"] == "sandbox-agent")
        rc = dep["spec"]["template"]["spec"].get("runtimeClassName")
        assert rc == "gvisor", f"sandbox-agent must set runtimeClassName: gvisor, got {rc!r}."

    def test_worker_has_invokable_bedrock_model(self, all_deployments: dict) -> None:
        """BEDROCK_DEFAULT_MODEL_ID must be set to a cross-region inference profile.

        Without it every /run that doesn't pass "model" returns
        errors.bedrock="no model configured". A bare foundation-model ID is also
        wrong: Anthropic models on Bedrock are INFERENCE_PROFILE-only (the
        ON_DEMAND Claude 3 is refused as provider-legacy), so the ID needs a
        `us.`/`global.` routing prefix.
        """
        dep = next(d for d in all_deployments["items"] if d["metadata"]["name"] == "sandbox-agent")
        env = {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        model = env.get("BEDROCK_DEFAULT_MODEL_ID", "")
        assert model, (
            "sandbox-agent must set BEDROCK_DEFAULT_MODEL_ID — set agent_sandbox.default_model_id in clusters.yaml."
        )
        assert model.startswith(("us.", "global.", "eu.", "apac.")), (
            f"BEDROCK_DEFAULT_MODEL_ID must be a cross-region inference profile ID, got {model!r}."
        )

    def test_requests_equal_limits(self, all_deployments: dict) -> None:
        """Guaranteed QoS: capacity per node must stay a division, not a guess.
        Requests below limits would let sandboxes overcommit the fleet node and
        burst into each other's CPU."""
        dep = next(d for d in all_deployments["items"] if d["metadata"]["name"] == "sandbox-agent")
        resources = dep["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["requests"] == resources["limits"], (
            f"sandbox-agent requests must equal limits; got {resources!r}."
        )

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


class TestAgentSandboxServiceAccounts:
    def test_sigv4_proxy_sa_has_irsa(self) -> None:
        """sigv4-proxy signs AWS/Bedrock with a read-only IRSA role."""
        sa = run_kubectl(["get", "serviceaccount", "sigv4-proxy"], namespace=NAMESPACE)
        ann = sa.get("metadata", {}).get("annotations", {})
        assert IRSA_KEY in ann, "sigv4-proxy SA missing IRSA annotation — deploy.sh did not annotate it."
        assert ann[IRSA_KEY].startswith("arn:aws:iam::"), f"IRSA annotation is not an IAM role ARN: {ann[IRSA_KEY]}"

    def test_agent_sa_is_credential_and_token_free(self) -> None:
        """The untrusted agent SA must NOT automount a K8s token and must have no
        IRSA role — the agent holds no credentials of any kind."""
        sa = run_kubectl(["get", "serviceaccount", "sandbox-agent"], namespace=NAMESPACE)
        assert sa.get("automountServiceAccountToken") is False, (
            "sandbox-agent SA must set automountServiceAccountToken: false."
        )
        ann = sa.get("metadata", {}).get("annotations", {})
        assert IRSA_KEY not in ann, "sandbox-agent SA must NOT carry an IRSA role — the agent gets no AWS identity."

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
    def test_default_deny_exists(self) -> None:
        nps = run_kubectl(["get", "networkpolicies"], namespace=NAMESPACE)
        names = {np["metadata"]["name"] for np in nps.get("items", [])}
        assert "default-deny" in names, f"agent-sandbox must ship a default-deny NetworkPolicy; got {sorted(names)}."
        assert len(names) >= 5, f"Expected the full NetworkPolicy set (>=5); got {sorted(names)}."
