"""Smoke tests for the agent-sandbox module.

Validates the deployed standing infrastructure: namespace, quota, the gvisor
RuntimeClass, the agent-sandbox controller and the task SandboxTemplate, the sigv4-proxy
that holds the only AWS credential, the dispatcher and their Services, the dispatcher's
namespaced RBAC, the credential-free sandbox-agent SA that task pods run as, and the
NetworkPolicies. These check the security spine is wired — not that an agent run
succeeds (that is the e2e test).

There is no standing worker to assert on: the dispatcher claims one pod per request, so
task pods exist only while a task runs. Their shape is asserted in the dispatcher's own
unit tests, against the SandboxTemplate — and here against the API server, which is the
only place the admission policy's CEL is actually evaluated.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml
from helpers import DEFAULT_TIMEOUT, assert_deployment_ready, filter_deployments, filter_services, run_kubectl

pytestmark = [pytest.mark.live]

NAMESPACE = "ai-sandbox"
DISPATCHER = f"system:serviceaccount:{NAMESPACE}:sandbox-dispatcher"
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
    check that the CEL compiles and that the API server actually denies. `kubectl get`
    succeeding says nothing either way — the object is accepted before any request has been
    evaluated against it — and under `failurePolicy: Fail` an expression that errors at
    evaluation time denies the request, so the symptom is an outage rather than a hole. The
    dry-run probes below are what settle that. `.status.typeChecking` is a separate,
    advisory, schema-level signal and is read with that distinction in mind.

    These are all CREATE probes. The nodeName rule's UPDATE branch cannot be reached from
    here — it needs a pod the scheduler has actually bound — and is covered by the
    integration test, which dispatches a real task and would hang out its deadline if a
    task pod could not be updated after binding.
    """

    POLICY = "agent-sandbox-task-pods"

    def _server_dry_run(self, manifest: str) -> subprocess.CompletedProcess:
        """Apply against the API server without persisting. Server-side dry-run runs the
        full admission chain, this policy included.

        Bounded, like run_kubectl: kubectl's own request timeout defaults to unbounded, so
        an API server that accepts the connection and never answers — or a kubeconfig exec
        credential plugin waiting on input — would park an xdist worker forever and hang
        `just smoke` rather than failing it."""
        return subprocess.run(
            ["kubectl", "-n", NAMESPACE, "apply", "--dry-run=server", "--request-timeout=60s", "-f", "-"],
            input=manifest,
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TIMEOUT,
        )

    def _task_pod(self, name: str, **spec_overrides: object) -> str:
        """A pod carrying the task label, so the Pod pass sees it. That pass is not
        scoped to the dispatcher's service account — pods are created by the agent-sandbox
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

    def test_policy_and_its_binding_are_deployed(self) -> None:
        policy = run_kubectl(["get", "validatingadmissionpolicy", self.POLICY])
        assert policy["spec"]["failurePolicy"] == "Fail"
        bound = {
            b["metadata"]["name"]
            for b in run_kubectl(["get", "validatingadmissionpolicybinding"])["items"]
            if b["spec"]["policyName"] == self.POLICY
        }
        assert bound == {self.POLICY}, f"the policy is inert without its binding; found {sorted(bound)}."

    def test_the_policy_type_checks(self) -> None:
        """Advisory — VAP evaluates `object` dynamically, so a warning here is not a
        denial — but the policy matches Pods alone, so every expression should check
        cleanly against the Pod schema."""
        policy = run_kubectl(["get", "validatingadmissionpolicy", self.POLICY])
        status = policy.get("status", {})
        assert status.get("observedGeneration") == policy["metadata"]["generation"], (
            "the API server has not finished type-checking this generation of the policy yet — "
            f"observedGeneration={status.get('observedGeneration')}, generation={policy['metadata']['generation']}."
        )
        warnings = status.get("typeChecking", {}).get("expressionWarnings") or []
        assert not warnings, f"CEL type-check warnings on {self.POLICY}: {warnings}"

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
            # The two rules whose CEL is not a shape already proven by the cases above: a
            # map-keyed `all`, and a bound on a field the API server defaults. Both are
            # unevaluated until something here denies with them.
            (
                "a device request",
                {
                    "containers": [
                        {
                            "name": "task",
                            "image": "harbor:30002/osdc/ci-agent-sandbox:admission-probe",
                            "securityContext": {"allowPrivilegeEscalation": False, "runAsNonRoot": True},
                            # Equal on both sides on purpose: Kubernetes requires that of
                            # an extended resource, so this satisfies the limits-present
                            # and Guaranteed-QoS rules and only the allowlist can say no.
                            "resources": {
                                "requests": {
                                    "cpu": "1",
                                    "memory": "1Gi",
                                    "ephemeral-storage": "1Gi",
                                    "nvidia.com/gpu": "1",
                                },
                                "limits": {
                                    "cpu": "1",
                                    "memory": "1Gi",
                                    "ephemeral-storage": "1Gi",
                                    "nvidia.com/gpu": "1",
                                },
                            },
                        }
                    ]
                },
            ),
            ("a long termination grace period", {"terminationGracePeriodSeconds": 3600}),
        ],
    )
    def test_the_policy_denies_what_it_says_it_denies(self, case: str, overrides: dict) -> None:
        name = "admission-probe-" + case.lower().replace(" ", "-")
        result = self._server_dry_run(self._task_pod(name, **overrides))
        assert result.returncode != 0, f"a task pod with {case} was ADMITTED — the policy is not enforcing."
        assert self.POLICY in result.stderr, (
            f"a task pod with {case} was rejected, but not by this policy: {result.stderr.strip()}"
        )


class TestClaimAdmissionPolicy:
    """What the dispatcher may put in a SandboxClaim, evaluated by the API server. Probed
    AS the dispatcher, because the policy only matches its writes; `create` rather than
    `apply`, because the dispatcher has no patch permission on claims."""

    POLICY = "agent-sandbox-task-claims"

    def _dry_run_as_dispatcher(self, name: str, **spec_overrides: object) -> subprocess.CompletedProcess:
        spec: dict = {
            "warmPoolRef": {"name": "sandbox-task"},
            "lifecycle": {"shutdownTime": "2099-01-01T00:00:00Z", "shutdownPolicy": "Delete"},
        }
        spec.update(spec_overrides)
        spec = {k: v for k, v in spec.items() if v is not None}
        manifest = yaml.safe_dump(
            {
                "apiVersion": "extensions.agents.x-k8s.io/v1beta1",
                "kind": "SandboxClaim",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": spec,
            }
        )
        return subprocess.run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "create",
                "--dry-run=server",
                "--request-timeout=60s",
                f"--as={DISPATCHER}",
                "-f",
                "-",
            ],
            input=manifest,
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_TIMEOUT,
        )

    def test_policy_is_deployed_and_type_checks(self) -> None:
        policy = run_kubectl(["get", "validatingadmissionpolicy", self.POLICY])
        assert policy["spec"]["failurePolicy"] == "Fail"
        status = policy.get("status", {})
        assert status.get("observedGeneration") == policy["metadata"]["generation"]
        warnings = status.get("typeChecking", {}).get("expressionWarnings") or []
        assert not warnings, f"CEL type-check warnings on {self.POLICY}: {warnings}"

    def test_the_claim_the_dispatcher_sends_is_admitted(self) -> None:
        result = self._dry_run_as_dispatcher("admission-probe-claim-good")
        assert result.returncode == 0, f"a compliant claim was rejected: {result.stderr.strip()}"

    @pytest.mark.parametrize(
        ("case", "overrides"),
        [
            ("another pool", {"warmPoolRef": {"name": "admission-probe"}}),
            ("baked-in env", {"env": [{"name": "SANDBOX_REPO", "value": "pytorch/pytorch"}]}),
            ("no expiry", {"lifecycle": None}),
        ],
    )
    def test_the_policy_denies_what_it_says_it_denies(self, case: str, overrides: dict) -> None:
        result = self._dry_run_as_dispatcher("admission-probe-claim-" + case.lower().replace(" ", "-"), **overrides)
        assert result.returncode != 0, f"a dispatcher claim with {case} was ADMITTED — the policy is not enforcing."
        assert self.POLICY in result.stderr, (
            f"a dispatcher claim with {case} was rejected, but not by this policy: {result.stderr.strip()}"
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


class TestGitProxy:
    """The proxy that holds the GitHub credential so task pods do not."""

    def test_git_proxy_ready(self, all_deployments: dict) -> None:
        """A crashlooping proxy is invisible from the deploy otherwise: private clones
        fail at task time with a connection refused rather than at rollout."""
        assert_deployment_ready(all_deployments, NAMESPACE, "git-proxy")

    def test_git_proxy_service_exists(self, all_services: dict) -> None:
        svcs = filter_services(all_services, namespace=NAMESPACE, name="git-proxy")
        assert len(svcs) == 1, f"Expected Service 'git-proxy' in '{NAMESPACE}'."

    def test_git_proxy_holds_no_kubernetes_identity(self, all_deployments: dict) -> None:
        """It holds a GitHub credential; it has no business holding a K8s one too."""
        dep = _deployment(all_deployments, "git-proxy")
        assert dep["spec"]["template"]["spec"].get("automountServiceAccountToken") is not True

    def test_the_task_pods_never_receive_the_credential(self, all_deployments: dict) -> None:
        """The whole point: the pod learns the proxy's ADDRESS, never its token."""
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        for entry in env:
            assert "git-proxy-credentials" not in str(entry.get("valueFrom", "")), (
                "the dispatcher must not mount the git credential; only git-proxy may"
            )


class TestSandboxDispatcher:
    """The callable entry point — a Deployment + Service reachable from arc-runners,
    like buildkitd. It claims one pod per request, so there is no standing worker: task
    pods exist only while a task runs, and their template is asserted in the
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

    def test_auth_is_required(self, all_deployments: dict) -> None:
        """A request with no OIDC token must be refused. "false" is a rollback switch; a
        cluster left on it has /run open to every pod in arc-runners."""
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        env = {e["name"]: e.get("value", "") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        assert env.get("REQUIRE_AUTH") == "true", f"REQUIRE_AUTH must be 'true'; got {env.get('REQUIRE_AUTH')!r}."

    def test_concurrency_is_capped(self, all_deployments: dict) -> None:
        """Authentication bounds who may call, not how often, so an uncapped dispatcher
        turns a caller loop into unbounded pods and, through Karpenter, unbounded nodes."""
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


class TestAgentSandboxController:
    """The upstream controller that will run task pods from SandboxClaims."""

    CONTROLLER_NS = "agent-sandbox-system"

    def test_controller_ready(self, all_deployments: dict) -> None:
        assert_deployment_ready(all_deployments, self.CONTROLLER_NS, "agent-sandbox-controller")

    def test_controller_is_not_on_the_sandbox_fleet(self, all_deployments: dict) -> None:
        """It holds cluster-wide pod-create RBAC, so it must not sit beside task pods."""
        deps = filter_deployments(all_deployments, namespace=self.CONTROLLER_NS, name="agent-sandbox-controller")
        assert len(deps) == 1, f"Expected the agent-sandbox-controller Deployment in {self.CONTROLLER_NS}."
        terms = deps[0]["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]
        assert {"key": "node-fleet", "operator": "NotIn", "values": ["ai-sandbox"]} in terms[0]["matchExpressions"]

    @pytest.mark.parametrize(
        "crd",
        [
            "sandboxes.agents.x-k8s.io",
            "sandboxtemplates.extensions.agents.x-k8s.io",
            "sandboxclaims.extensions.agents.x-k8s.io",
            "sandboxwarmpools.extensions.agents.x-k8s.io",
        ],
    )
    def test_crds_are_installed(self, crd: str) -> None:
        run_kubectl(["get", "crd", crd])


class TestTaskTemplate:
    """The task pod as the controller will build it: sandboxd in the task image."""

    def _container(self) -> dict:
        template = run_kubectl(["get", "sandboxtemplate", "sandbox-task"], namespace=NAMESPACE)
        return template["spec"]["podTemplate"]["spec"]["containers"][0]

    def test_the_warm_pool_serves_the_template(self) -> None:
        pool = run_kubectl(["get", "sandboxwarmpool", "sandbox-task"], namespace=NAMESPACE)
        assert pool["spec"]["sandboxTemplateRef"]["name"] == "sandbox-task"

    def test_template_is_substituted(self) -> None:
        """Unsubstituted, the image rule rejects every task pod, and sandboxd accepts no
        token at all."""
        container = self._container()
        assert "__" not in container["image"], f"task image not substituted: {container['image']!r}"
        assert not any("__" in a for a in container["args"]), f"sandboxd args not substituted: {container['args']}"

    def test_template_has_invokable_bedrock_model(self) -> None:
        """Without a default model every /run that doesn't pass "model" returns
        errors.bedrock="no model configured". A bare foundation-model ID is also wrong:
        Anthropic models on Bedrock are INFERENCE_PROFILE-only, so the ID needs a
        `us.`/`global.` routing prefix — set agent_sandbox.default_model_id in clusters.yaml.
        """
        env = {e["name"]: e.get("value", "") for e in self._container().get("env", [])}
        model = env.get("BEDROCK_DEFAULT_MODEL_ID", "")
        assert model.startswith(("us.", "global.", "eu.", "apac.")), (
            f"BEDROCK_DEFAULT_MODEL_ID must be a cross-region inference profile ID, got {model!r}."
        )

    def test_sandboxd_token_exists(self) -> None:
        secret = run_kubectl(["get", "secret", "sandboxd-token"], namespace=NAMESPACE)
        assert secret.get("data", {}).get("token"), "secret/sandboxd-token has no token"


class TestAgentSandboxQuota:
    def test_quota_bounds_the_namespace(self) -> None:
        """The bound that holds even if a dispatcher is buggy: without it, pods claimed
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
        """The agent-sandbox controller creates the pods. A dispatcher that could create
        pods directly could create one without the gvisor RuntimeClass."""
        role = run_kubectl(["get", "role", "sandbox-dispatcher"], namespace=NAMESPACE)
        pod_verbs = {v for rule in role["rules"] if "pods" in rule.get("resources", []) for v in rule["verbs"]}
        assert pod_verbs <= {"get", "list", "watch"}, f"pods must be read-only for the dispatcher; got {pod_verbs}."

    def test_dispatcher_can_create_claims_here_and_nowhere_else(self) -> None:
        claims = "sandboxclaims.extensions.agents.x-k8s.io"
        assert self._can_i("create", claims, DISPATCHER, NAMESPACE) == "yes"
        assert self._can_i("create", claims, DISPATCHER, "default") == "no"

    def test_dispatcher_cannot_write_a_pod_spec(self) -> None:
        """A claim names the template; a Sandbox or a SandboxTemplate would carry a pod
        spec, so neither is the dispatcher's to write."""
        for resource in ("sandboxes.agents.x-k8s.io", "sandboxtemplates.extensions.agents.x-k8s.io", "pods"):
            assert self._can_i("create", resource, DISPATCHER, NAMESPACE) == "no", resource

    def test_dispatcher_cannot_create_jobs(self) -> None:
        """Jobs carry a pod spec, which is exactly what the dispatcher must not write."""
        assert self._can_i("create", "jobs", DISPATCHER, NAMESPACE) == "no"

    def test_dispatcher_cannot_read_secrets(self) -> None:
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-dispatcher"
        assert self._can_i("get", "secrets", subject, NAMESPACE) == "no"

    def test_task_service_account_cannot_create_jobs(self) -> None:
        """Task pods run as sandbox-agent, which must stay unable to touch the API at
        all — otherwise the untrusted side can launch its own pods."""
        subject = f"system:serviceaccount:{NAMESPACE}:sandbox-agent"
        assert self._can_i("create", "jobs", subject, NAMESPACE) == "no"
        assert self._can_i("create", "sandboxclaims.extensions.agents.x-k8s.io", subject, NAMESPACE) == "no"


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
        # Exactly the two credential-holding proxies, and an EQUALITY check rather than a
        # subset one: this assertion exists to catch a third peer being added, so it has
        # to fail when the set grows.
        peers = {
            t["podSelector"]["matchLabels"]["app"] for r in pod_rules for t in r.get("to", []) if "podSelector" in t
        }
        assert peers == {"sigv4-proxy", "git-proxy"}, (
            f"task pods may reach only the sigv4 and git proxies; got {sorted(peers)}."
        )

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
        able to reach the internet — only DNS, the API server, and task pods' sandboxd."""
        np = run_kubectl(["get", "networkpolicy", "sandbox-dispatcher-egress"], namespace=NAMESPACE)
        rules = np["spec"].get("egress", [])
        ports = {(p.get("protocol", "TCP"), p["port"]) for rule in rules for p in rule.get("ports", [])}
        assert ports == {("UDP", 53), ("TCP", 53), ("TCP", 443), ("TCP", 9090)}, (
            f"dispatcher egress must be DNS, the API server and sandboxd; got {sorted(ports)}."
        )
        cidrs = {t["ipBlock"]["cidr"] for rule in rules for t in rule.get("to", []) if "ipBlock" in t}
        assert cidrs, "dispatcher egress must name the API server address."
        assert not {c for c in cidrs if c in ("0.0.0.0/0", "::/0")}, (
            f"dispatcher egress must not include a default route — that is an exfiltration path; got {cidrs}."
        )
        sandboxd_rule = next(r for r in rules if any(p["port"] == 9090 for p in r.get("ports", [])))
        assert sandboxd_rule["to"] == [{"podSelector": {"matchLabels": {"app": "sandbox-task"}}}]

    def test_only_the_dispatcher_can_reach_sandboxd(self) -> None:
        """sandboxd runs whatever it is sent; this is what keeps one task pod from
        running code in another."""
        np = run_kubectl(["get", "networkpolicy", "sandbox-task-ingress"], namespace=NAMESPACE)
        assert np["spec"]["podSelector"] == {"matchLabels": {"app": "sandbox-task"}}
        assert np["spec"]["ingress"] == [
            {
                "from": [{"podSelector": {"matchLabels": {"app": "sandbox-dispatcher"}}}],
                "ports": [{"protocol": "TCP", "port": 9090}],
            }
        ]

    def test_dispatcher_mounts_the_sandboxd_token(self, all_deployments: dict) -> None:
        dep = _deployment(all_deployments, "sandbox-dispatcher")
        volumes = dep["spec"]["template"]["spec"].get("volumes", [])
        assert any(v.get("secret", {}).get("secretName") == "sandboxd-token" for v in volumes), (
            "the dispatcher must mount secret/sandboxd-token to call task pods' sandboxd"
        )
