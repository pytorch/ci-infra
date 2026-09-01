"""Smoke tests for the agent-sandbox module.

Validates the deployed standing infrastructure: namespace, quota, the gvisor
RuntimeClass, the sigv4-proxy that holds the only AWS credential, the dispatcher and
their Services, the dispatcher's namespaced RBAC, the credential-free sandbox-agent SA
that task pods run as, and the NetworkPolicies. These check the security spine is wired
— not that an agent run succeeds (that is the e2e test).

There is no standing worker to assert on: the dispatcher creates one Job per request, so
task pods exist only while a task runs. Their shape is asserted in the dispatcher's own
unit tests, against the Job manifest it builds — and here against the API server, which
is the only place the admission policy's CEL is actually evaluated.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml
from helpers import assert_deployment_ready, filter_deployments, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "ai-sandbox"
IRSA_KEY = "eks.amazonaws.com/role-arn"
PROXY_PORT = 8080


def _deployment(all_deployments: dict, name: str) -> dict:
    """A Deployment in this namespace, by name.

    Filtering on namespace matters: a bare name match over a cluster-wide list would
    silently assert against a same-named Deployment elsewhere, and would raise
    StopIteration rather than say what is missing when the module isn't deployed.
    """
    deps = filter_deployments(all_deployments, namespace=NAMESPACE, name=name)
    assert len(deps) == 1, f"Expected exactly one '{name}' Deployment in '{NAMESPACE}'; got {len(deps)}."
    return deps[0]


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


class TestTaskAdmissionPolicy:
    """The cluster-side copy of the task-pod isolation contract.

    The dispatcher's unit tests check that the two copies agree; only a live cluster can
    check that the CEL compiles and that the API server actually denies. A policy whose
    expressions fail to type-check is still created and reports the failure in
    `.status.typeChecking` — under `failurePolicy: Fail` such a rule then denies every
    matching request at runtime, so the symptom is an outage rather than a hole, and
    `kubectl get` succeeding says nothing either way. The probes below are the assertion.

    These are all CREATE probes. The nodeName rule's UPDATE branch cannot be reached from
    here — it needs a pod the scheduler has actually bound — and is covered by the
    integration test, which dispatches a real task and would hang out its deadline if a
    task pod could not be updated after binding.
    """

    POLICY = "agent-sandbox-task-jobs"

    def _server_dry_run(self, manifest: str) -> subprocess.CompletedProcess:
        """Apply against the API server without persisting. Server-side dry-run runs the
        full admission chain, this policy included."""
        return subprocess.run(
            ["kubectl", "-n", NAMESPACE, "apply", "--dry-run=server", "-f", "-"],
            input=manifest,
            capture_output=True,
            text=True,
            check=False,
        )

    def _task_pod(self, name: str, **spec_overrides: object) -> str:
        """A pod carrying the task label, so the Pod pass sees it. That pass is not
        scoped to the dispatcher's service account — pods are created by the Job
        controller — which is what lets this run under the smoke suite's own identity."""
        spec: dict = {
            "runtimeClassName": "gvisor",
            "serviceAccountName": "sandbox-agent",
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "task",
                    "image": "harbor:30002/osdc/ci-agent-sandbox:admission-probe",
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "runAsNonRoot": True,
                    },
                    "resources": {
                        "requests": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"},
                        "limits": {"cpu": "1", "memory": "1Gi", "ephemeral-storage": "1Gi"},
                    },
                }
            ],
        }
        spec.update(spec_overrides)
        # None means "omit the field", not "set it to null" — the rules that matter here
        # are `has(x) && ...`, and absence is the case they exist to reject.
        spec = {k: v for k, v in spec.items() if v is not None}
        return yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": {"app": "sandbox-task"}},
                "spec": spec,
            }
        )

    def test_policy_and_both_bindings_are_deployed(self) -> None:
        policy = run_kubectl(["get", "validatingadmissionpolicy", self.POLICY])
        assert policy["spec"]["failurePolicy"] == "Fail"
        bound = {
            b["metadata"]["name"]
            for b in run_kubectl(["get", "validatingadmissionpolicybinding"])["items"]
            if b["spec"]["policyName"] == self.POLICY
        }
        assert bound == {"agent-sandbox-task-jobs", "agent-sandbox-task-pods"}, (
            f"the policy needs both its bindings to be enforced on Jobs and on Pods; found {sorted(bound)}."
        )

    def test_the_policy_type_checks(self) -> None:
        """A type error does not stop the policy being created; it is reported here, and
        at runtime it becomes a denial of every matching request."""
        policy = run_kubectl(["get", "validatingadmissionpolicy", self.POLICY])
        status = policy.get("status", {})
        assert status.get("observedGeneration") == policy["metadata"]["generation"], (
            "the API server has not finished type-checking this generation of the policy yet — "
            f"observedGeneration={status.get('observedGeneration')}, generation={policy['metadata']['generation']}."
        )
        assert "typeChecking" in status, "no typeChecking result on an observed policy generation"
        failures = status["typeChecking"].get("expressionWarnings") or []
        assert not failures, f"CEL type-check warnings on {self.POLICY}: {failures}"

    def test_a_compliant_task_pod_is_admitted(self) -> None:
        """The positive half. Without it a policy that denies everything — a broken
        expression under failurePolicy: Fail — would look like a passing negative test."""
        result = self._server_dry_run(self._task_pod("admission-probe-good"))
        assert result.returncode == 0, f"a compliant task pod was rejected: {result.stderr.strip()}"

    # Each violation is chosen so that NOTHING ELSE in the admission chain would reject
    # it first: no `runtimeClassName: runc` (there is no runc RuntimeClass to resolve, so
    # the RuntimeClass admission plugin would answer before this policy did), and an
    # emptyDir rather than a hostPath (Pod Security would answer first). Dropping a
    # required field, or adding a volume type nothing else objects to, leaves this policy
    # as the only thing that can say no.
    @pytest.mark.parametrize(
        ("case", "overrides"),
        [
            ("no gvisor", {"runtimeClassName": None}),
            ("a mounted token", {"automountServiceAccountToken": True}),
            ("a volume", {"volumes": [{"name": "scratch", "emptyDir": {}}]}),
            ("a pinned nodeName", {"nodeName": "ip-10-0-0-1.ec2.internal"}),
        ],
    )
    def test_the_policy_denies_what_it_says_it_denies(self, case: str, overrides: dict) -> None:
        name = "admission-probe-" + case.lower().replace(" ", "-")
        result = self._server_dry_run(self._task_pod(name, **overrides))
        assert result.returncode != 0, f"a task pod with {case} was ADMITTED — the policy is not enforcing."
        assert "agent-sandbox-task-jobs" in result.stderr, (
            f"a task pod with {case} was rejected, but not by this policy: {result.stderr.strip()}"
        )


class TestSigv4Proxy:
    """The proxy is what makes the sandbox credential-free: it holds the IRSA role
    and signs on the agent's behalf."""

    def test_sigv4_proxy_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, "sigv4-proxy")

    def test_proxy_service_exists(self, all_services: dict) -> None:
        svcs = filter_services(all_services, namespace=NAMESPACE, name="sigv4-proxy")
        assert len(svcs) == 1, f"Expected Service 'sigv4-proxy' in '{NAMESPACE}'."

    def test_proxy_is_pinned_to_bedrock(self, all_deployments: dict) -> None:
        """Without --host/--name the caller's Host header decides which AWS service
        gets signed for, leaving the IAM policy as the only boundary — and the caller
        is the untrusted sandbox. --verbose would put the live security token in pod
        logs, which alloy-logging ships to Loki."""
        deps = filter_deployments(all_deployments, namespace=NAMESPACE, name="sigv4-proxy")
        assert len(deps) == 1, f"Expected exactly one 'sigv4-proxy' Deployment in '{NAMESPACE}'."
        args = deps[0]["spec"]["template"]["spec"]["containers"][0].get("args", [])
        for flag in ("--host", "--name", "--region"):
            assert flag in args, f"sigv4-proxy must pin {flag}; got {args}."
        host = args[args.index("--host") + 1]
        assert host.startswith("bedrock-runtime."), f"sigv4-proxy --host must be bedrock-runtime.*; got {host!r}."
        service = args[args.index("--name") + 1]
        assert service == "bedrock", f"sigv4-proxy --name must be 'bedrock'; got {service!r}."
        assert "--verbose" not in args, "sigv4-proxy must not run --verbose — it logs the signed credential."


class TestSandboxDispatcher:
    """The callable entry point — a Deployment + Service reachable from arc-runners,
    like buildkitd. It creates one Job per request, so there is no standing worker: task
    pods exist only while a task runs, and the Job manifest is asserted in the
    dispatcher's unit tests."""

    def test_dispatcher_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, NAMESPACE, "sandbox-dispatcher")

    def test_service_keeps_the_sandbox_agent_name(self, all_services: dict) -> None:
        """sandbox-agent.ai-sandbox.svc:8080 is the address arc-runners and the canary
        already use; the dispatcher answers there now."""
        svcs = filter_services(all_services, namespace=NAMESPACE, name="sandbox-agent")
        assert len(svcs) == 1, f"Expected Service 'sandbox-agent' in '{NAMESPACE}'."
        assert svcs[0]["spec"]["selector"] == {"app": "sandbox-dispatcher"}, (
            f"the sandbox-agent Service must select the dispatcher; got {svcs[0]['spec']['selector']!r}."
        )

    def test_dispatcher_is_not_on_the_sandbox_fleet(self, all_deployments: dict) -> None:
        """It holds the RBAC that creates task pods, so it must not run under gVisor
        alongside the untrusted work it launches."""
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        assert dep["spec"]["template"]["spec"].get("runtimeClassName") is None, (
            "sandbox-dispatcher must not set runtimeClassName — the sandbox fleet is for task pods."
        )

    def test_dispatcher_knows_the_task_image(self, all_deployments: dict) -> None:
        """AGENT_IMAGE is what it stamps into every Job. Unsubstituted, /run answers 500
        rather than creating a Job that cannot pull."""
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        env = {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        image = env.get("AGENT_IMAGE", "")
        assert image, "sandbox-dispatcher must set AGENT_IMAGE — it stamps that image into every Job."
        assert "__" not in image, f"AGENT_IMAGE must be substituted by deploy.sh; got {image!r}."

    def test_dispatcher_has_invokable_bedrock_model(self, all_deployments: dict) -> None:
        """BEDROCK_DEFAULT_MODEL_ID is passed to every task pod. Without it every /run
        that doesn't pass "model" returns errors.bedrock="no model configured". A bare
        foundation-model ID is also wrong: Anthropic models on Bedrock are
        INFERENCE_PROFILE-only (the ON_DEMAND Claude 3 is refused as provider-legacy),
        so the ID needs a `us.`/`global.` routing prefix.
        """
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        env = {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        model = env.get("BEDROCK_DEFAULT_MODEL_ID", "")
        assert model, (
            "sandbox-dispatcher must set BEDROCK_DEFAULT_MODEL_ID — set agent_sandbox.default_model_id in clusters.yaml."
        )
        assert model.startswith(("us.", "global.", "eu.", "apac.")), (
            f"BEDROCK_DEFAULT_MODEL_ID must be a cross-region inference profile ID, got {model!r}."
        )

    def test_concurrency_is_capped(self, all_deployments: dict) -> None:
        """/run is unauthenticated, so an uncapped dispatcher turns a caller loop into
        unbounded Jobs and, through Karpenter, unbounded nodes."""
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        env = {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        cap = env.get("MAX_CONCURRENT_TASKS", "")
        assert cap.isdigit(), f"MAX_CONCURRENT_TASKS must be an integer; got {cap!r}."
        assert int(cap) > 0, f"MAX_CONCURRENT_TASKS must be positive; got {cap!r}."

    def test_callable_from_arc_runners(self) -> None:
        """A NetworkPolicy must allow arc-runners to reach the dispatcher (buildkitd
        parity: network access, not RBAC, is the entry point)."""
        np = run_kubectl(["get", "networkpolicy", "sandbox-agent-ingress"], namespace=NAMESPACE)
        froms = [f for rule in np["spec"].get("ingress", []) for f in rule.get("from", [])]
        allowed_ns = {
            f.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") for f in froms
        }
        assert "arc-runners" in allowed_ns, (
            f"sandbox-agent-ingress must allow the arc-runners namespace; got {allowed_ns}."
        )


class TestAgentSandboxQuota:
    def test_quota_bounds_the_namespace(self) -> None:
        """The bound that holds even if a dispatcher is buggy: without it, Jobs created
        per request become ai-sandbox nodes until an AWS quota notices."""
        rq = run_kubectl(["get", "resourcequota", "ai-sandbox"], namespace=NAMESPACE)
        hard = rq["spec"]["hard"]
        for key in ("pods", "requests.cpu", "requests.memory", "requests.ephemeral-storage"):
            assert key in hard, f"ResourceQuota must bound {key}; got {sorted(hard)}."


class TestDispatcherRBAC:
    """Creating a pod is privileged, so the dispatcher's permissions are the trust shift
    this design introduces — and they must stay namespaced and minimal."""

    def _can_i(self, verb: str, resource: str, subject: str, namespace: str) -> str:
        proc = subprocess.run(
            ["kubectl", "-n", namespace, "auth", "can-i", verb, resource, f"--as={subject}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip()

    def test_role_is_namespaced(self) -> None:
        role = run_kubectl(["get", "role", "sandbox-dispatcher"], namespace=NAMESPACE)
        assert role["metadata"]["namespace"] == NAMESPACE

    def test_role_grants_no_write_on_pods(self) -> None:
        """The Job controller creates the pods. A dispatcher that could create pods
        directly could create one without the gvisor RuntimeClass."""
        role = run_kubectl(["get", "role", "sandbox-dispatcher"], namespace=NAMESPACE)
        pod_verbs = {v for rule in role["rules"] if "pods" in rule.get("resources", []) for v in rule["verbs"]}
        assert pod_verbs <= {"get", "list", "watch"}, f"pods must be read-only for the dispatcher; got {pod_verbs}."

    def test_dispatcher_can_create_jobs_here(self) -> None:
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-dispatcher"
        assert self._can_i("create", "jobs", subject, NAMESPACE) == "yes"

    def test_dispatcher_cannot_create_jobs_elsewhere(self) -> None:
        """Namespaced Role, not a ClusterRole: a compromised dispatcher must not be able
        to run pods outside the sandbox namespace."""
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-dispatcher"
        assert self._can_i("create", "jobs", subject, "default") == "no"

    def test_dispatcher_cannot_read_secrets(self) -> None:
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-dispatcher"
        assert self._can_i("get", "secrets", subject, NAMESPACE) == "no"

    def test_task_service_account_cannot_create_jobs(self) -> None:
        """Task pods run as sandbox-agent, which must stay unable to touch the API at
        all — otherwise the untrusted side can launch its own pods."""
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-agent"
        assert self._can_i("create", "jobs", subject, NAMESPACE) == "no"


class TestAgentSandboxServiceAccounts:
    def test_sigv4_proxy_sa_has_irsa(self) -> None:
        """sigv4-proxy signs AWS/Bedrock with a read-only IRSA role."""
        sa = run_kubectl(["get", "serviceaccount", "sigv4-proxy"], namespace=NAMESPACE)
        ann = sa.get("metadata", {}).get("annotations", {})
        assert IRSA_KEY in ann, (
            "sigv4-proxy SA missing IRSA annotation — deploy.sh did not substitute __SIGV4_ROLE_ARN__."
        )
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

    def test_task_egress_allow_list(self) -> None:
        """The whole design rests on this allow-list, so assert its contents rather
        than a count of policy names: widening it to 0.0.0.0/0 on all ports, or
        loosening the podSelector, leaves every name in place and a count green."""
        np = run_kubectl(["get", "networkpolicy", "sandbox-task-egress"], namespace=NAMESPACE)
        assert np["spec"].get("podSelector", {}).get("matchLabels") == {"app": "sandbox-task"}, (
            f"sandbox-task-egress must select only task pods; got {np['spec'].get('podSelector')!r}."
        )
        rules = np["spec"].get("egress", [])
        assert all(rule.get("ports") for rule in rules), (
            f"every worker egress rule must name ports — an unported rule allows all of them; got {rules}."
        )
        ports = {(p.get("protocol", "TCP"), p["port"]) for rule in rules for p in rule.get("ports", [])}
        assert ports == {("TCP", PROXY_PORT), ("UDP", 53), ("TCP", 53), ("TCP", 443)}, (
            f"task egress must be the proxy, DNS and HTTPS for the git clone; got {sorted(ports)}."
        )

        pod_rules = [r for r in rules if any("podSelector" in t for t in r.get("to", []))]
        assert pod_rules, "task egress must allow the sigv4-proxy."
        assert all(
            t["podSelector"]["matchLabels"] == {"app": "sigv4-proxy"} for r in pod_rules for t in r.get("to", [])
        ), f"the only pod-to-pod egress may be the sigv4-proxy; got {pod_rules}."

        # The clone path: NetworkPolicy can't name github.com, so this is the widest
        # rule in the namespace and must stay HTTPS-only.
        wide_rules = [r for r in rules if any("ipBlock" in t for t in r.get("to", []))]
        assert wide_rules, "task egress must express the git clone path explicitly, not rely on unenforced IPv4."
        wide_ports = {(p.get("protocol", "TCP"), p["port"]) for r in wide_rules for p in r.get("ports", [])}
        assert wide_ports == {("TCP", 443)}, (
            f"the internet-facing task rule must be HTTPS-only (the clone); got {sorted(wide_ports)}."
        )

    def test_dispatcher_egress_has_no_internet_path(self) -> None:
        """The dispatcher is the component with RBAC, so it is the one that must not be
        able to reach the internet — only DNS and the API server."""
        np = run_kubectl(["get", "networkpolicy", "sandbox-dispatcher-egress"], namespace=NAMESPACE)
        rules = np["spec"].get("egress", [])
        ports = {(p.get("protocol", "TCP"), p["port"]) for rule in rules for p in rule.get("ports", [])}
        assert ports == {("UDP", 53), ("TCP", 53), ("TCP", 443)}, (
            f"dispatcher egress must be DNS plus the API server; got {sorted(ports)}."
        )
        cidrs = {t["ipBlock"]["cidr"] for rule in rules for t in rule.get("to", []) if "ipBlock" in t}
        assert cidrs, "dispatcher egress must name the API server address."
        assert not {c for c in cidrs if c in ("0.0.0.0/0", "::/0")}, (
            f"dispatcher egress must not include a default route — that is an exfiltration path; got {cidrs}."
        )
