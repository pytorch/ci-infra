"""The task admission policies, checked against the pod and claim this module really makes.

admissionpolicy.yaml is a second copy of the isolation contract that sandbox-template.yaml
already spells out, written in CEL and enforced by the API server. Two copies of one
contract drift, and the way they drift is silent: the template changes, the cluster starts
rejecting every task pod, and the first symptom is a dispatch error in production.

So each CEL validation is mirrored here as a Python predicate, keyed by the policy's own
message string. The test then asserts three things: every validation in the YAML has a
mirror (adding a CEL rule without one fails here), the real template pod satisfies all of
them, and each rule actually rejects the thing it names — a mirror that accepts everything
would otherwise pass quietly.

Keying on the message catches a rule ADDED without a mirror, and a message reworded away
from its mirror. It does NOT catch the likeliest drift of all: an expression edited in
place with its message left alone. EXPRESSION_DIGESTS closes that — every expression is
pinned by digest, so editing one fails here until someone re-reads the mirror beside it
and re-pins. That is the whole point of the extra step; it is meant to be mildly annoying.

The mirrors are not a CEL evaluator and this file does not claim the expressions compile;
that is the API server's job on first apply. What it does claim is that the two
descriptions of a task pod have not diverged.
"""

import copy
import datetime
import hashlib
import re
from pathlib import Path

import kube
import pytest
import yaml

MODULE = Path(__file__).resolve().parent.parent
POLICY_PATH = MODULE / "kubernetes" / "base" / "admissionpolicy.yaml"
DEPLOY_SH = MODULE / "deploy.sh"
DISPATCHER_MANIFEST = MODULE / "kubernetes" / "base" / "dispatcher.yaml"
KUSTOMIZATION = MODULE / "kubernetes" / "base" / "kustomization.yaml"
TEMPLATE_PATH = MODULE / "kubernetes" / "base" / "sandbox-template.yaml"


def _deployed_image_repository() -> str:
    """The repository deploy.sh actually pushes the task image to.

    Read rather than restated. The repository reaches a task pod from `IMAGE=` in
    deploy.sh and the policy names it again in a CEL literal; a third hand-written copy
    here would make the image rule the one rule checked against the test's own opinion.
    Change `IMAGE=` alone and the policy rejects every task pod — in production, with this
    suite green.
    """
    match = re.search(r'^IMAGE="([^"]+)"', DEPLOY_SH.read_text(), re.M)
    assert match, f"no IMAGE= assignment found in {DEPLOY_SH} — the image rule has nothing to check against"
    return match.group(1)


IMAGE_REPOSITORY = _deployed_image_repository()
# The tag deploy.sh substitutes is a content hash; only the repository is pinned.
GOOD_IMAGE = f"{IMAGE_REPOSITORY}:0123456789ab"


@pytest.fixture(scope="module")
def documents():
    return list(yaml.safe_load_all(POLICY_PATH.read_text()))


POD_POLICY = "agent-sandbox-task-pods"
CLAIM_POLICY = "agent-sandbox-task-claims"


def _policy(documents, name):
    return next(d for d in documents if d["kind"] == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == name)


def _bindings(documents, name):
    return [d for d in documents if d["kind"] == "ValidatingAdmissionPolicyBinding" and d["spec"]["policyName"] == name]


@pytest.fixture(scope="module")
def policy(documents):
    """The task-pod policy."""
    return _policy(documents, POD_POLICY)


@pytest.fixture(scope="module")
def bindings(documents):
    return _bindings(documents, POD_POLICY)


@pytest.fixture(scope="module")
def claim_policy(documents):
    return _policy(documents, CLAIM_POLICY)


def _template_pod(image: str = GOOD_IMAGE) -> dict:
    """The SandboxTemplate's pod, with the image deploy.sh would substitute."""
    template = next(d for d in yaml.safe_load_all(TEMPLATE_PATH.read_text()) if d["kind"] == "SandboxTemplate")
    pod = copy.deepcopy(template["spec"]["podTemplate"])
    for c in pod["spec"]["containers"]:
        c["image"] = c["image"].replace("__AGENT_IMAGE__", image)
    return pod


def a_pod(**spec_overrides) -> dict:
    """The real template pod with its spec optionally mutated."""
    pod = _template_pod()
    pod["spec"].update(spec_overrides)
    return pod


def _pod(pod):
    return pod["spec"]


def _all_containers(pod):
    return _pod(pod)["containers"] + _pod(pod).get("initContainers", [])


def _nodename_rule(spec: dict, *, operation: str) -> bool:
    """The one rule whose meaning depends on the REQUEST and not only on the object.

    By the time a task pod is updated the scheduler has already set spec.nodeName through
    pods/binding. A rule of `!has(nodeName)` therefore denied every write to a running
    task pod — the controller's own label and status writes among them.
    """
    return "nodeName" not in spec or operation == "UPDATE"


# Each entry mirrors one `validations:` item, keyed by the exact `message:` string in the
# YAML, and paired with a pod the rule must reject. Keeping the message as the key is what
# makes drift loud: reword a message in the policy and this test names the rule that lost
# its mirror.
MIRRORS = {
    "task pods must set runtimeClassName: gvisor (this is also what confines them to the ai-sandbox fleet)": (
        lambda p: _pod(p).get("runtimeClassName") == "gvisor",
        lambda: a_pod(runtimeClassName="runc"),
    ),
    "task pods must run as serviceAccountName: sandbox-agent, which holds no RBAC": (
        lambda p: _pod(p).get("serviceAccountName") == "sandbox-agent",
        lambda: a_pod(serviceAccountName="sandbox-dispatcher"),
    ),
    "task pods must set automountServiceAccountToken: false — a mounted token is API access from inside the sandbox": (
        lambda p: _pod(p).get("automountServiceAccountToken") is False,
        lambda: a_pod(automountServiceAccountToken=True),
    ),
    "task pods must not share host namespaces (hostNetwork/hostPID/hostIPC)": (
        lambda p: not any(_pod(p).get(k) for k in ("hostNetwork", "hostPID", "hostIPC")),
        lambda: a_pod(hostPID=True),
    ),
    "task pods must not pin nodeName — it bypasses the scheduler, and with it the RuntimeClass node selector": (
        lambda p: _nodename_rule(_pod(p), operation="CREATE"),
        lambda: a_pod(nodeName="ip-10-0-0-1"),
    ),
    "task pods must use the default scheduler — an alternate scheduler need not honour the RuntimeClass node selector": (
        lambda p: _pod(p).get("schedulerName", "default-scheduler") == "default-scheduler",
        lambda: a_pod(schedulerName="my-scheduler"),
    ),
    "task pods must declare no volumes — see the module README before adding one": (
        lambda p: not _pod(p).get("volumes"),
        lambda: a_pod(volumes=[{"name": "host", "hostPath": {"path": "/"}}]),
    ),
    "task pods must claim no devices — a resource claim can attach host hardware and its mounts": (
        lambda p: not _pod(p).get("resourceClaims"),
        lambda: a_pod(resourceClaims=[{"name": "gpu", "resourceClaimName": "gpu"}]),
    ),
    "task pods must set no sysctls": (
        lambda p: not _pod(p).get("securityContext", {}).get("sysctls"),
        lambda: a_pod(securityContext={"sysctls": [{"name": "net.core.somaxconn", "value": "1024"}]}),
    ),
    "task pods must declare no init containers": (
        lambda p: not _pod(p).get("initContainers"),
        lambda: a_pod(initContainers=[{"name": "sidecar", "image": GOOD_IMAGE, "restartPolicy": "Always"}]),
    ),
    "task containers must run the task image from harbor:30002/osdc/ci-agent-sandbox": (
        lambda p: all(c["image"].startswith(f"{IMAGE_REPOSITORY}:") for c in _all_containers(p)),
        lambda: a_pod(containers=[{**a_pod()["spec"]["containers"][0], "image": "docker.io/alpine"}]),
    ),
    "a task pod runs exactly one container": (
        lambda p: len(_pod(p)["containers"]) == 1,
        lambda: a_pod(containers=a_pod()["spec"]["containers"] * 2),
    ),
    "task containers must not use envFrom — it pulls a whole Secret or ConfigMap into the sandbox": (
        lambda p: all(not c.get("envFrom") for c in _all_containers(p)),
        lambda: _with_container(envFrom=[{"secretRef": {"name": "bedrock"}}]),
    ),
    "task container env must be literal values — valueFrom reads Secrets, ConfigMaps and pod fields into the sandbox": (
        lambda p: all(all("valueFrom" not in e for e in c.get("env", [])) for c in _all_containers(p)),
        lambda: _with_container(env=[{"name": "X", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]),
    ),
    "task containers must set allowPrivilegeEscalation: false and runAsNonRoot: true, and must not be privileged": (
        lambda p: all(
            c.get("securityContext", {}).get("allowPrivilegeEscalation") is False
            and c.get("securityContext", {}).get("privileged") in (None, False)
            and c.get("securityContext", {}).get("runAsNonRoot") is True
            for c in _all_containers(p)
        ),
        lambda: _with_container(
            securityContext={"privileged": True, "runAsNonRoot": True, "allowPrivilegeEscalation": False}
        ),
    ),
    "task containers must not add Linux capabilities": (
        lambda p: all(not c.get("securityContext", {}).get("capabilities", {}).get("add") for c in _all_containers(p)),
        lambda: _with_container(
            securityContext={
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"add": ["SYS_ADMIN"]},
            }
        ),
    ),
    "task containers must not unmask /proc": (
        lambda p: all(
            c.get("securityContext", {}).get("procMount", "Default") == "Default" for c in _all_containers(p)
        ),
        lambda: _with_container(
            securityContext={"runAsNonRoot": True, "allowPrivilegeEscalation": False, "procMount": "Unmasked"}
        ),
    ),
    "task containers must not publish a hostPort": (
        lambda p: all("hostPort" not in p for c in _all_containers(p) for p in c.get("ports", [])),
        lambda: _with_container(ports=[{"containerPort": 8080, "hostPort": 8080}]),
    ),
    "task containers must set cpu, memory and ephemeral-storage limits": (
        lambda p: all(
            {"cpu", "memory", "ephemeral-storage"} <= set(c.get("resources", {}).get("limits", {}))
            for c in _all_containers(p)
        ),
        lambda: _with_container(resources={"requests": {"cpu": "2"}}),
    ),
    "task containers must limit cpu, memory and ephemeral-storage and nothing else — any other key is an unapproved resource, device requests among them": (
        lambda p: all(
            set(c.get("resources", {}).get("limits", {})) <= {"cpu", "memory", "ephemeral-storage"}
            for c in _all_containers(p)
        ),
        # requests mirrors limits on purpose: Kubernetes requires that of an extended
        # resource, so this counterexample satisfies both the rule above and Guaranteed
        # QoS, and only this rule can reject it. That is the whole finding.
        lambda: _with_container(
            resources={
                "requests": {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "50Gi", "nvidia.com/gpu": "1"},
                "limits": {"cpu": "2", "memory": "8Gi", "ephemeral-storage": "50Gi", "nvidia.com/gpu": "1"},
            }
        ),
    ),
    "task pods must not set terminationGracePeriodSeconds above 60 — it is how long a deleted task pod may keep its node": (
        lambda p: _pod(p).get("terminationGracePeriodSeconds", 30) <= 60,
        lambda: a_pod(terminationGracePeriodSeconds=3600),
    ),
    "task containers must request exactly what they limit (Guaranteed QoS)": (
        lambda p: all(c["resources"]["requests"] == c["resources"]["limits"] for c in _all_containers(p)),
        lambda: _with_container(resources={"requests": {"cpu": "1"}, "limits": {"cpu": "8"}}),
    ),
}


def _with_container(**container_overrides) -> dict:
    """A pod whose single container carries the given overrides."""
    pod = a_pod()
    _pod(pod)["containers"][0].update(container_overrides)
    return pod


def test_validation_messages_are_distinct(policy):
    """Both coverage tables are keyed by message, so a repeat is a rule that loses both.

    MIRRORS and EXPRESSION_DIGESTS are dicts keyed by the `message:` string, and the two
    tests below them compare KEY SETS. Give two validations the same message and each
    table keeps one entry for the pair, both set comparisons still pass, and one of the
    two rules is left with no Python mirror and no pinned expression — which is the
    coverage those tests exist to guarantee. What fails, if anything does, depends on
    which of the two came last in the file. This is the assertion that does not.
    """
    messages = [v["message"] for v in policy["spec"]["validations"]]
    duplicates = sorted({m for m in messages if messages.count(m) > 1})
    assert not duplicates, (
        "two validations share a message, so MIRRORS and EXPRESSION_DIGESTS each hold one "
        f"entry for the pair and neither key-set comparison can see it: {duplicates}"
    )


def test_every_validation_has_a_mirror(policy):
    """A CEL rule added without a Python mirror is a rule this file silently stops covering."""
    messages = {v["message"] for v in policy["spec"]["validations"]}
    assert messages == set(MIRRORS), {
        "unmirrored (add to MIRRORS)": sorted(messages - set(MIRRORS)),
        "stale (no longer in the policy)": sorted(set(MIRRORS) - messages),
    }


@pytest.mark.parametrize("message", sorted(MIRRORS))
def test_the_task_template_pod_satisfies_the_policy(message):
    """What sandbox-template.yaml declares today would be admitted."""
    predicate, _ = MIRRORS[message]
    assert predicate(a_pod()), f"the SandboxTemplate pod violates: {message}"


@pytest.mark.parametrize("message", sorted(MIRRORS))
def test_each_rule_rejects_its_own_violation(message):
    """A mirror that accepts everything passes the test above for the wrong reason."""
    predicate, violating = MIRRORS[message]
    assert not predicate(violating()), f"mirror does not actually enforce: {message}"


@pytest.mark.parametrize(
    ("spec", "operation", "admitted"),
    [
        ({}, "CREATE", True),
        ({"nodeName": "ip-10-0-0-1"}, "CREATE", False),
        # The scheduler has bound the pod, so nodeName is set on every later UPDATE.
        ({"nodeName": "ip-10-0-0-1"}, "UPDATE", True),
    ],
)
def test_the_nodename_rule_admits_a_bound_pod_on_update(spec, operation, admitted):
    """Exempting UPDATE costs nothing: spec.nodeName is immutable through the pods main
    resource, so the only writer is the pods/binding subresource this policy never
    matched. Pinning it at CREATE is the whole of what the rule ever enforced."""
    assert _nodename_rule(spec, operation=operation) is admitted


def test_bindings_deny_rather_than_warn(policy, bindings):
    """A policy with no binding, or one set to Warn, enforces nothing while looking applied."""
    assert bindings, "the policy has no binding at all — it would be inert"
    for b in bindings:
        assert b["spec"]["policyName"] == policy["metadata"]["name"]
        assert b["spec"]["validationActions"] == ["Deny"]
        assert b["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "ai-sandbox"
        }


def test_the_pod_pass_selects_the_label_the_template_stamps(bindings):
    """The Pod pass is label-scoped, so a label drift in the template would silently
    switch it off rather than fail anything — hence this test rather than a comment."""
    (binding,) = bindings
    selector = binding["spec"]["matchResources"]["objectSelector"]["matchLabels"]
    template_labels = a_pod()["metadata"]["labels"]
    assert selector.items() <= template_labels.items(), (
        f"pod binding selects {selector}, but the template stamps {template_labels}"
    )


def test_the_kustomization_does_not_rewrite_the_dispatcher_identity():
    """Why reading the identity off the source manifest is sound TODAY.

    The test above derives `system:serviceaccount:<ns>:<sa>` from dispatcher.yaml, which is
    the deployed value only because the base applies the manifests as written. Add a
    `namespace:` or a name prefix here and the cluster sees a subject the policy's
    matchCondition does not name — the claims pass switches off silently, and deriving
    from source would go on agreeing with itself.

    Scope, stated rather than implied: this is a denylist of the directives that can rewrite
    an identity, so it is a tripwire on the deployment path staying simple, NOT a proof that
    no rendering can change it. The complete answer is to render the kustomization and compare
    the rendered Deployment against the rendered policy condition, which needs a kustomize
    binary this unit suite does not have.
    """
    kustomization = yaml.safe_load(KUSTOMIZATION.read_text())
    # Case-insensitively: kustomize decodes through Go's JSON decoder, which matches field
    # names without regard to case, so a `Namespace:` key would apply and an exact-match
    # denylist would not see it.
    declared = {k.lower(): v for k, v in kustomization.items()}
    rewriters = sorted(
        k
        for k in (
            "namespace",
            "namePrefix",
            "nameSuffix",
            "patches",
            "patchesStrategicMerge",
            "patchesJson6902",
            "replacements",
            "components",
            "transformers",
            # `nameReference` is transformer CONFIG, not a top-level key — but
            # `configurations` is what loads one, and a custom ConfigMap nameReference
            # pointed at spec/template/spec/serviceAccountName rewrites the identity with
            # every other directive here untouched. `vars` is the deprecated route to the same.
            "configurations",
            "vars",
            # Deprecated, but still accepted, and it takes a DIRECTORY — the same nested
            # route the resources check below closes.
            "bases",
        )
        if declared.get(k.lower())
    )
    assert not rewriters, (
        f"{KUSTOMIZATION.name} now applies {rewriters}, which can change the identity the "
        "dispatcher presents — derive it from the rendered output instead of dispatcher.yaml"
    )

    # A directory entry is a NESTED kustomization, which carries its own transformers — it
    # could apply `namespace:` to the Deployment while every check above passes and
    # dispatcher.yaml still reads the old subject. Requiring a bare filename in THIS
    # directory, not merely something `is_file()` resolves, is what keeps the manifest this
    # test reads and the manifest that deploys as the same file: `sub/x.yaml`, an absolute
    # path and a symlink out of the tree all satisfy `is_file()` and none of them would.
    def is_local_manifest(entry: str) -> bool:
        path = KUSTOMIZATION.parent / entry
        return entry == Path(entry).name and path.is_file() and not path.is_symlink()

    resources = declared.get("resources") or []
    nested = sorted(e for e in resources if not is_local_manifest(e))
    assert not nested, (
        f"{KUSTOMIZATION.name} pulls in {nested}, which is not a plain manifest in this "
        "directory — a nested kustomization can rewrite the identity out of this test's sight"
    )
    # The check above bounds what MAY be deployed; this one is what makes the two manifests
    # the test reads the ones that ARE. Swap dispatcher.yaml for a same-directory
    # dispatcher-v2.yaml carrying a different subject and everything above stays green while
    # the identity test goes on reading a file nobody applies.
    for manifest in (DISPATCHER_MANIFEST, POLICY_PATH):
        assert manifest.name in resources, (
            f"{manifest.name} is no longer in {KUSTOMIZATION.name}'s resources, so the file "
            "the identity tests read is not the file this module deploys"
        )


def test_policy_fails_closed_and_covers_pods(policy):
    assert policy["spec"]["failurePolicy"] == "Fail"
    # Membership for the resources list only, so adding `pods/ephemeralcontainers` — the
    # change this policy file prescribes — does not make this claim the Pod pass vanished.
    # apiGroups and operations stay exact: widening either should be read, not absorbed.
    assert any(
        r["apiGroups"] == [""] and "pods" in r["resources"] and set(r["operations"]) == {"CREATE", "UPDATE"}
        for r in policy["spec"]["matchConstraints"]["resourceRules"]
    ), "the Pod pass matches nothing"


def test_the_pod_pass_is_unconditional(policy):
    """Pods are created by the agent-sandbox controller, not the dispatcher, so a userInfo
    condition of the claims policy's kind would switch the Pod pass off entirely."""
    assert "matchConditions" not in policy["spec"]


# Every rule's expression, pinned by digest of its EXACT parsed text — see _digest(). The
# alternative, and it is a genuine trade rather than a strict improvement: hashing the
# whitespace-NORMALISED expression instead tolerates reformatting, at the price of being
# blind to whitespace inside a quoted CEL literal, where 'a  b' -> 'a b' IS semantic.
#
# Exact hashing is kept because it fails in the safe direction — a re-indented
# continuation line inside a folded scalar is a spurious re-pin prompt, never a missed
# edit — and because normalising would go quietly wrong the day a quoted CEL literal
# gains a space. Revisit deliberately, not by accident.
#
# The MIRRORS table above is keyed by MESSAGE, which cannot see an expression edited in
# place with its message untouched — and that is the drift most likely to actually happen,
# because tightening a rule rarely changes what it is called. Pinning the expression means
# such an edit fails here, and the only way to make it pass is to open the mirror beside it
# and decide whether it still describes the rule. Re-pin deliberately, never by pasting the
# digest the failure prints without reading the diff it is telling you about.
EXPRESSION_DIGESTS = {
    "task pods must set runtimeClassName: gvisor (this is also what confines them to the ai-sandbox fleet)": "93161384aabc",
    "task pods must run as serviceAccountName: sandbox-agent, which holds no RBAC": "2779cb7d8a6b",
    "task pods must set automountServiceAccountToken: false — a mounted token is API access from inside the sandbox": "224303faac17",
    "task pods must not pin nodeName — it bypasses the scheduler, and with it the RuntimeClass node selector": "8e885c64824f",
    "task pods must use the default scheduler — an alternate scheduler need not honour the RuntimeClass node selector": "d76e4a690cdc",
    "task pods must not share host namespaces (hostNetwork/hostPID/hostIPC)": "1cafda1b80d7",
    "task pods must declare no volumes — see the module README before adding one": "0ad2da654e90",
    "task pods must claim no devices — a resource claim can attach host hardware and its mounts": "164b13576611",
    "task pods must set no sysctls": "64b91d2f9f8f",
    "task containers must run the task image from harbor:30002/osdc/ci-agent-sandbox": "2d6ca10e9119",
    "a task pod runs exactly one container": "88ba5a9fb243",
    "task pods must declare no init containers": "37fef9251087",
    "task containers must not use envFrom — it pulls a whole Secret or ConfigMap into the sandbox": "89e7123f9143",
    "task container env must be literal values — valueFrom reads Secrets, ConfigMaps and pod fields into the sandbox": "794a441e820d",
    "task containers must set allowPrivilegeEscalation: false and runAsNonRoot: true, and must not be privileged": "8c0bbaf088a3",
    "task containers must not add Linux capabilities": "d15fed6ab652",
    "task containers must not unmask /proc": "f9d3102f1226",
    "task containers must not publish a hostPort": "aadf2d231816",
    "task containers must set cpu, memory and ephemeral-storage limits": "82ea58d521a8",
    "task containers must limit cpu, memory and ephemeral-storage and nothing else — any other key is an unapproved resource, device requests among them": "cbe9db10c667",
    "task containers must request exactly what they limit (Guaranteed QoS)": "e5824532099d",
    "task pods must not set terminationGracePeriodSeconds above 60 — it is how long a deleted task pod may keep its node": "4490bf5bfaf0",
}


# The expressions every rule is READ THROUGH. EXPRESSION_DIGESTS covers `validations`
# only, so without this a rewrite of `variables.pod` would silently change the meaning of
# every rule that reads it.
VARIABLE_DIGESTS = {
    "pod": "4cfabaaed490",
    "allContainers": "b41ce25c2638",
}


def _digest(expression: str) -> str:
    """The EXACT parsed expression, not a whitespace-normalised one.

    Normalising first would have collapsed whitespace inside CEL string literals too, so
    a real semantic change like 'a  b' -> 'a b' would keep its digest.

    The price is that reformatting is no longer free, and the expressions where it costs
    most are exactly the ones this change had to re-pin. YAML folds a `>-` scalar's single
    newlines into single spaces, so re-wrapping usually leaves the value identical — but a
    run of whitespace the old normalisation collapsed now survives into the digest, and a
    more-indented line inside a folded scalar keeps its newline instead of folding. So
    read a digest failure as "re-read the mirror beside this rule", never as "the
    formatter moved something and the pin needs bumping".
    """
    return hashlib.sha256(expression.encode()).hexdigest()[:12]


def test_no_variable_changed_without_being_rechecked(policy):
    """Every rule reads its pod spec through `variables.pod`; nothing else pins it."""
    live = {v["name"]: _digest(v["expression"]) for v in policy["spec"]["variables"]}
    assert live == VARIABLE_DIGESTS, {
        "changed or unpinned": sorted(k for k in live if VARIABLE_DIGESTS.get(k) != live[k]),
        "stale (no longer in the policy)": sorted(set(VARIABLE_DIGESTS) - set(live)),
    }


def test_no_expression_changed_without_its_mirror_being_rechecked(policy):
    """Keying the mirrors by message leaves in-place expression edits invisible."""
    live = {v["message"]: _digest(v["expression"]) for v in policy["spec"]["validations"]}
    drifted = {
        message: digest
        for message, digest in live.items()
        if message in EXPRESSION_DIGESTS and EXPRESSION_DIGESTS[message] != digest
    }
    assert not drifted, (
        "the CEL expression changed but its message did not, so nothing else in this file "
        f"would have noticed: {sorted(drifted)}. Re-read each rule's Python mirror, then "
        f"re-pin: {drifted}"
    )
    assert set(live) == set(EXPRESSION_DIGESTS), {
        "unpinned (add to EXPRESSION_DIGESTS)": sorted(set(live) - set(EXPRESSION_DIGESTS)),
        "stale (no longer in the policy)": sorted(set(EXPRESSION_DIGESTS) - set(live)),
    }


def test_an_unsubstituted_task_image_is_rejected():
    """A deploy.sh that failed to substitute leaves the literal `__AGENT_IMAGE__` in the
    template; an empty substitution leaves "". Both must be rejected."""
    predicate, _ = next(
        mirror for message, mirror in MIRRORS.items() if message.startswith("task containers must run the task image")
    )
    for value in ("", "__AGENT_IMAGE__"):
        assert not predicate(_template_pod(image=value)), f"an image of {value!r} must not be admitted"


def test_the_policy_pins_the_repository_deploy_sh_actually_pushes_to(policy):
    """The third copy of the repository, and the one nothing else compares."""
    rule = next(
        v for v in policy["spec"]["validations"] if v["message"].startswith("task containers must run the task image")
    )
    assert f"'{IMAGE_REPOSITORY}:'" in rule["expression"], (
        f"the image rule pins {rule['expression']}, but deploy.sh pushes to {IMAGE_REPOSITORY} — "
        "every task pod would be rejected"
    )
    assert rule["message"].endswith(IMAGE_REPOSITORY), "the rejection message names a repository nobody deploys"


def _dispatcher_deployment() -> dict:
    return next(
        d
        for d in yaml.safe_load_all(DISPATCHER_MANIFEST.read_text())
        if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "sandbox-dispatcher"
    )


def _deployed_dispatcher_identity() -> str:
    """The API-server username the dispatcher actually presents, read off its Deployment.

    Read from the source manifest rather than from a rendered kustomization, which is only
    sound while the base applies no identity-changing transformer.
    test_the_kustomization_does_not_rewrite_the_dispatcher_identity is a tripwire on the
    directives that could — a denylist, not a proof that none can.
    """
    deployment = _dispatcher_deployment()
    namespace = deployment["metadata"]["namespace"]
    service_account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
    return f"system:serviceaccount:{namespace}:{service_account}"


def test_the_jwks_configmap_ships_with_no_data(documents):
    """Seeding it in the manifest made every deploy blank the live signing keys: apply
    reasserts what the manifest declares, so the seed overwrote whatever the refresher had
    written. The content belongs to the refresher; declaring any `data` here brings the
    bug straight back."""
    oidc_path = POLICY_PATH.parent / "oidc.yaml"
    configmap = next(
        d
        for d in yaml.safe_load_all(oidc_path.read_text())
        if d and d["kind"] == "ConfigMap" and d["metadata"]["name"] == "oidc-jwks"
    )
    assert "data" not in configmap, (
        "oidc-jwks must declare no data — see the comment above it in oidc.yaml before changing this"
    )


# --- The claims policy: what the dispatcher may put in a SandboxClaim ----------------------
#
# Same scheme as the pod rules above: each validation mirrored by message, checked against
# what kube.claim_manifest() really builds, made to reject its own counterexample, and its
# expression pinned by digest.


def a_claim(**spec_overrides) -> dict:
    """claim_manifest() as the policy sees it: the API server has set creationTimestamp."""
    now = datetime.datetime.now(datetime.UTC)
    claim = kube.claim_manifest("abc123456789", now)
    claim["metadata"]["creationTimestamp"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    claim["spec"].update(spec_overrides)
    return claim


def _claim_lifetime_s(claim: dict) -> float:
    shutdown = datetime.datetime.fromisoformat(claim["spec"]["lifecycle"]["shutdownTime"])
    created = datetime.datetime.fromisoformat(claim["metadata"]["creationTimestamp"])
    return (shutdown - created).total_seconds()


CLAIM_MIRRORS = {
    "task claims must draw from the sandbox-task warm pool": (
        lambda c: c["spec"]["warmPoolRef"]["name"] == "sandbox-task",
        lambda: a_claim(warmPoolRef={"name": "some-other-pool"}),
    ),
    "task claims must inject no env — the task's inputs go over the sandboxd call": (
        lambda c: not c["spec"].get("env"),
        lambda: a_claim(env=[{"name": "SANDBOX_REPO", "value": "pytorch/pytorch"}]),
    ),
    "task claims must request no volumes": (
        lambda c: not c["spec"].get("volumeClaimTemplates"),
        lambda: a_claim(volumeClaimTemplates=[{"metadata": {"name": "data"}, "spec": {}}]),
    ),
    "task claims must add no pod labels or annotations": (
        lambda c: not any(c["spec"].get("additionalPodMetadata", {}).get(k) for k in ("labels", "annotations")),
        lambda: a_claim(additionalPodMetadata={"labels": {"sandbox.users.io/app": "x"}}),
    ),
    "task claims must set a shutdownTime with shutdownPolicy: Delete": (
        lambda c: (
            bool(c["spec"].get("lifecycle", {}).get("shutdownTime"))
            and c["spec"]["lifecycle"].get("shutdownPolicy") == "Delete"
        ),
        lambda: a_claim(lifecycle={"shutdownPolicy": "Retain"}),
    ),
    "task claims must shut down at most 3600s after they are created": (
        lambda c: "shutdownTime" not in c["spec"].get("lifecycle", {}) or _claim_lifetime_s(c) <= 3600,
        lambda: a_claim(lifecycle={"shutdownTime": "2099-01-01T00:00:00Z", "shutdownPolicy": "Delete"}),
    ),
}

CLAIM_EXPRESSION_DIGESTS = {
    "task claims must draw from the sandbox-task warm pool": "d42db9fc7d38",
    "task claims must inject no env — the task's inputs go over the sandboxd call": "52fb84bdc236",
    "task claims must request no volumes": "8c86a51e84f6",
    "task claims must add no pod labels or annotations": "4355e9e17f8a",
    "task claims must set a shutdownTime with shutdownPolicy: Delete": "dc1389d78fe6",
    "task claims must shut down at most 3600s after they are created": "5eab990674d3",
}
CLAIM_MATCH_CONDITION_DIGESTS = {"claim-writes-come-from-the-dispatcher": "d4aa846ba0d8"}


def test_every_claim_validation_has_a_mirror_and_a_pin(claim_policy):
    messages = [v["message"] for v in claim_policy["spec"]["validations"]]
    assert len(messages) == len(set(messages)), "two claim validations share a message"
    assert set(messages) == set(CLAIM_MIRRORS) == set(CLAIM_EXPRESSION_DIGESTS)
    live = {v["message"]: _digest(v["expression"]) for v in claim_policy["spec"]["validations"]}
    assert live == CLAIM_EXPRESSION_DIGESTS, (
        "a claim rule's CEL changed: re-read its mirror in CLAIM_MIRRORS, then re-pin"
    )


@pytest.mark.parametrize("message", sorted(CLAIM_MIRRORS))
def test_the_claim_the_dispatcher_builds_satisfies_the_policy(message):
    predicate, _ = CLAIM_MIRRORS[message]
    assert predicate(a_claim()), f"claim_manifest() violates: {message}"


@pytest.mark.parametrize("message", sorted(CLAIM_MIRRORS))
def test_each_claim_rule_rejects_its_own_violation(message):
    predicate, violating = CLAIM_MIRRORS[message]
    assert not predicate(violating()), f"mirror does not actually enforce: {message}"


def test_the_claim_pass_is_scoped_to_the_dispatcher_and_enforced(documents, claim_policy):
    """The controller writes claims too; the dispatcher's writes are the threat. The
    subject is derived from the Deployment rather than restated: drift here fails open,
    admitting every claim unchecked."""
    assert claim_policy["spec"]["failurePolicy"] == "Fail"
    conditions = claim_policy["spec"]["matchConditions"]
    assert {c["name"]: _digest(c["expression"]) for c in conditions} == CLAIM_MATCH_CONDITION_DIGESTS
    assert f"'{_deployed_dispatcher_identity()}'" in conditions[0]["expression"]
    (binding,) = _bindings(documents, CLAIM_POLICY)
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert "objectSelector" not in binding["spec"]["matchResources"], "a claim with no label would skip it"
    assert binding["spec"]["matchResources"]["resourceRules"][0]["resources"] == ["sandboxclaims"]
