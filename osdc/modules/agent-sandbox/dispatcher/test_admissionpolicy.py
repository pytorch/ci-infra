"""The task-Job admission policy, checked against the Job the dispatcher actually builds.

admissionpolicy.yaml is a second copy of the isolation contract that job_manifest()
already implements, written in CEL and enforced by the API server. Two copies of one
contract drift, and the way they drift is silent: job_manifest() changes, the cluster
starts rejecting every task, and the first symptom is a dispatch error in production.

So each CEL validation is mirrored here as a Python predicate, keyed by the policy's own
message string. The test then asserts three things: every validation in the YAML has a
mirror (adding a CEL rule without one fails here), the real job_manifest() output
satisfies all of them, and each rule actually rejects the thing it names — a mirror that
accepts everything would otherwise pass quietly.

Keying on the message catches a rule ADDED without a mirror, and a message reworded away
from its mirror. It does NOT catch the likeliest drift of all: an expression edited in
place with its message left alone. EXPRESSION_DIGESTS closes that — every expression is
pinned by digest, so editing one fails here until someone re-reads the mirror beside it
and re-pins. That is the whole point of the extra step; it is meant to be mildly annoying.

The mirrors are not a CEL evaluator and this file does not claim the expressions compile;
that is the API server's job on first apply. What it does claim is that the two
descriptions of a task pod have not diverged.
"""

import hashlib
import re
from pathlib import Path

import authorize
import kube
import pytest
import yaml

MODULE = Path(__file__).resolve().parent.parent
POLICY_PATH = MODULE / "kubernetes" / "base" / "admissionpolicy.yaml"
DEPLOY_SH = MODULE / "deploy.sh"
DISPATCHER_MANIFEST = MODULE / "kubernetes" / "base" / "dispatcher.yaml"


def _deployed_image_repository() -> str:
    """The repository deploy.sh actually pushes the task image to.

    Read rather than restated. The repository reaches a task pod from `IMAGE=` in
    deploy.sh and the policy names it again in a CEL literal; a third hand-written copy
    here would make the image rule the one rule checked against the test's own opinion.
    Change `IMAGE=` alone and the policy rejects every Job — in production, with this
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


@pytest.fixture(scope="module")
def policy(documents):
    return next(d for d in documents if d["kind"] == "ValidatingAdmissionPolicy")


@pytest.fixture(scope="module")
def bindings(documents):
    return [d for d in documents if d["kind"] == "ValidatingAdmissionPolicyBinding"]


@pytest.fixture(autouse=True)
def deployed_image(monkeypatch):
    """The task image the dispatcher was deployed with.

    Set here rather than written over the manifest afterwards. a_job() used to overwrite
    containers[0]["image"] on the way out, which meant the image rule was the one rule
    never checked against a value job_manifest() actually produced — it was checked
    against the fixture. AGENT_IMAGE defaults to "" in a test process, and "" is exactly
    what a deploy.sh that failed to substitute the tag would emit, so the old fixture hid
    the one input the rule exists to reject.
    """
    monkeypatch.setattr(kube, "AGENT_IMAGE", GOOD_IMAGE)


def a_job(**pod_overrides) -> dict:
    """A real job_manifest() with the pod spec optionally mutated."""
    grant = authorize.Grant(
        caller="pytorch/ciforge",
        workflow_ref="pytorch/ciforge/.github/workflows/x.yml@refs/heads/main",
        clone_repo="pytorch/pytorch",
        model="",
        task="hello",
        ref="",
    )
    job = kube.job_manifest("abc123456789", grant)
    job["spec"]["template"]["spec"].update(pod_overrides)
    return job


def _pod(job):
    return job["spec"]["template"]["spec"]


def _all_containers(job):
    return _pod(job)["containers"] + _pod(job).get("initContainers", [])


def _nodename_rule(pod: dict, *, is_job: bool, operation: str) -> bool:
    """The one rule whose meaning depends on the REQUEST and not only on the object.

    Every other mirror is a function of a Job, which is why none of them could see the
    bug this signature exists for: the Pod binding matches UPDATE, and by the time a task
    pod is updated the scheduler has already set spec.nodeName through pods/binding. A
    rule of `!has(nodeName)` therefore denied every write to a running task pod — among
    them the Job controller's finalizer removal, without which status.succeeded never
    reaches 1 and the dispatcher waits out its whole deadline on a task that finished.
    """
    return "nodeName" not in pod or (not is_job and operation == "UPDATE")


# Each entry mirrors one `validations:` item, keyed by the exact `message:` string in the
# YAML, and paired with a Job the rule must reject. Keeping the message as the key is what
# makes drift loud: reword a message in the policy and this test names the rule that lost
# its mirror.
MIRRORS = {
    "task pods must set runtimeClassName: gvisor (this is also what confines them to the ai-sandbox fleet)": (
        lambda j: _pod(j).get("runtimeClassName") == "gvisor",
        lambda: a_job(runtimeClassName="runc"),
    ),
    "task pods must run as serviceAccountName: sandbox-agent, which holds no RBAC": (
        lambda j: _pod(j).get("serviceAccountName") == "sandbox-agent",
        lambda: a_job(serviceAccountName="sandbox-dispatcher"),
    ),
    "task pods must set automountServiceAccountToken: false — a mounted token is API access from inside the sandbox": (
        lambda j: _pod(j).get("automountServiceAccountToken") is False,
        lambda: a_job(automountServiceAccountToken=True),
    ),
    "task pods must not share host namespaces (hostNetwork/hostPID/hostIPC)": (
        lambda j: not any(_pod(j).get(k) for k in ("hostNetwork", "hostPID", "hostIPC")),
        lambda: a_job(hostPID=True),
    ),
    "task pods must not pin nodeName — it bypasses the scheduler, and with it the RuntimeClass node selector": (
        lambda j: _nodename_rule(_pod(j), is_job=True, operation="CREATE"),
        lambda: a_job(nodeName="ip-10-0-0-1"),
    ),
    "task pods must use the default scheduler — an alternate scheduler need not honour the RuntimeClass node selector": (
        lambda j: _pod(j).get("schedulerName", "default-scheduler") == "default-scheduler",
        lambda: a_job(schedulerName="my-scheduler"),
    ),
    "task pods must declare no volumes — see the module README before adding one": (
        lambda j: not _pod(j).get("volumes"),
        lambda: a_job(volumes=[{"name": "host", "hostPath": {"path": "/"}}]),
    ),
    "task pods must claim no devices — a resource claim can attach host hardware and its mounts": (
        lambda j: not _pod(j).get("resourceClaims"),
        lambda: a_job(resourceClaims=[{"name": "gpu", "resourceClaimName": "gpu"}]),
    ),
    "task pods must set no sysctls": (
        lambda j: not _pod(j).get("securityContext", {}).get("sysctls"),
        lambda: a_job(securityContext={"sysctls": [{"name": "net.core.somaxconn", "value": "1024"}]}),
    ),
    "task pods must declare no init containers": (
        lambda j: not _pod(j).get("initContainers"),
        lambda: a_job(initContainers=[{"name": "sidecar", "image": GOOD_IMAGE, "restartPolicy": "Always"}]),
    ),
    "task containers must run the task image from harbor:30002/osdc/ci-agent-sandbox": (
        lambda j: all(c["image"].startswith(f"{IMAGE_REPOSITORY}:") for c in _all_containers(j)),
        lambda: a_job(
            containers=[{**a_job()["spec"]["template"]["spec"]["containers"][0], "image": "docker.io/alpine"}]
        ),
    ),
    "a task pod runs exactly one container": (
        lambda j: len(_pod(j)["containers"]) == 1,
        lambda: a_job(containers=a_job()["spec"]["template"]["spec"]["containers"] * 2),
    ),
    "task containers must not use envFrom — it pulls a whole Secret or ConfigMap into the sandbox": (
        lambda j: all(not c.get("envFrom") for c in _all_containers(j)),
        lambda: _with_container(envFrom=[{"secretRef": {"name": "bedrock"}}]),
    ),
    "task container env must be literal values — valueFrom reads Secrets, ConfigMaps and pod fields into the sandbox": (
        lambda j: all(all("valueFrom" not in e for e in c.get("env", [])) for c in _all_containers(j)),
        lambda: _with_container(env=[{"name": "X", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}}]),
    ),
    "task containers must set allowPrivilegeEscalation: false and runAsNonRoot: true, and must not be privileged": (
        lambda j: all(
            c.get("securityContext", {}).get("allowPrivilegeEscalation") is False
            and c.get("securityContext", {}).get("privileged") in (None, False)
            and c.get("securityContext", {}).get("runAsNonRoot") is True
            for c in _all_containers(j)
        ),
        lambda: _with_container(
            securityContext={"privileged": True, "runAsNonRoot": True, "allowPrivilegeEscalation": False}
        ),
    ),
    "task containers must not add Linux capabilities": (
        lambda j: all(not c.get("securityContext", {}).get("capabilities", {}).get("add") for c in _all_containers(j)),
        lambda: _with_container(
            securityContext={
                "runAsNonRoot": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"add": ["SYS_ADMIN"]},
            }
        ),
    ),
    "task containers must not unmask /proc": (
        lambda j: all(
            c.get("securityContext", {}).get("procMount", "Default") == "Default" for c in _all_containers(j)
        ),
        lambda: _with_container(
            securityContext={"runAsNonRoot": True, "allowPrivilegeEscalation": False, "procMount": "Unmasked"}
        ),
    ),
    "task containers must not publish a hostPort": (
        lambda j: all("hostPort" not in p for c in _all_containers(j) for p in c.get("ports", [])),
        lambda: _with_container(ports=[{"containerPort": 8080, "hostPort": 8080}]),
    ),
    "task containers must set cpu, memory and ephemeral-storage limits": (
        lambda j: all(
            {"cpu", "memory", "ephemeral-storage"} <= set(c.get("resources", {}).get("limits", {}))
            for c in _all_containers(j)
        ),
        lambda: _with_container(resources={"requests": {"cpu": "2"}}),
    ),
    "task Jobs must set activeDeadlineSeconds, at most 3600 — an unbounded task holds a fleet node and bills for it": (
        lambda j: 0 < j["spec"].get("activeDeadlineSeconds", 0) <= 3600,
        lambda: _without_job_field("activeDeadlineSeconds"),
    ),
    "task Jobs must run one pod at a time (parallelism: 1)": (
        lambda j: j["spec"].get("parallelism", 1) == 1,
        lambda: _with_job_field(parallelism=20),
    ),
    "task Jobs must run exactly one pod (completions: 1)": (
        lambda j: j["spec"].get("completions", 1) == 1,
        lambda: _with_job_field(completions=20),
    ),
    "task Jobs must set backoffLimit: 0 — a retry re-clones and re-prompts the model, and bills for it": (
        lambda j: j["spec"].get("backoffLimit") == 0,
        lambda: _with_job_field(backoffLimit=6),
    ),
    "task Job pod templates must carry the app: sandbox-task label": (
        lambda j: j["spec"]["template"]["metadata"]["labels"].get("app") == "sandbox-task",
        lambda: _with_template_labels({"app": "something-else"}),
    ),
    "task containers must request exactly what they limit (Guaranteed QoS)": (
        lambda j: all(c["resources"]["requests"] == c["resources"]["limits"] for c in _all_containers(j)),
        lambda: _with_container(resources={"requests": {"cpu": "1"}, "limits": {"cpu": "8"}}),
    ),
}


def _with_container(**container_overrides) -> dict:
    """A Job whose single container carries the given overrides."""
    job = a_job()
    _pod(job)["containers"][0].update(container_overrides)
    return job


def _with_job_field(**job_overrides) -> dict:
    job = a_job()
    job["spec"].update(job_overrides)
    return job


def _with_template_labels(labels: dict) -> dict:
    job = a_job()
    job["spec"]["template"]["metadata"]["labels"] = labels
    return job


def _without_job_field(name: str) -> dict:
    job = a_job()
    del job["spec"][name]
    return job


def test_every_validation_has_a_mirror(policy):
    """A CEL rule added without a Python mirror is a rule this file silently stops covering."""
    messages = {v["message"] for v in policy["spec"]["validations"]}
    assert messages == set(MIRRORS), {
        "unmirrored (add to MIRRORS)": sorted(messages - set(MIRRORS)),
        "stale (no longer in the policy)": sorted(set(MIRRORS) - messages),
    }


@pytest.mark.parametrize("message", sorted(MIRRORS))
def test_real_job_manifest_satisfies_the_policy(message):
    """What kube.job_manifest() builds today would be admitted."""
    predicate, _ = MIRRORS[message]
    assert predicate(a_job()), f"job_manifest() violates: {message}"


@pytest.mark.parametrize("message", sorted(MIRRORS))
def test_each_rule_rejects_its_own_violation(message):
    """A mirror that accepts everything passes the test above for the wrong reason."""
    predicate, violating = MIRRORS[message]
    assert not predicate(violating()), f"mirror does not actually enforce: {message}"


@pytest.mark.parametrize(
    ("pod", "is_job", "operation", "admitted"),
    [
        ({}, True, "CREATE", True),
        ({"nodeName": "ip-10-0-0-1"}, True, "CREATE", False),
        ({}, False, "CREATE", True),
        ({"nodeName": "ip-10-0-0-1"}, False, "CREATE", False),
        # The quadrant the Job-shaped mirrors structurally cannot reach, and the one that
        # broke: the scheduler has bound the pod, so nodeName is set on every subsequent
        # UPDATE the Pod binding sees.
        ({"nodeName": "ip-10-0-0-1"}, False, "UPDATE", True),
    ],
)
def test_the_nodename_rule_admits_a_bound_pod_on_update(pod, is_job, operation, admitted):
    """Exempting UPDATE costs nothing: spec.nodeName is immutable through the pods main
    resource, so the only writer is the pods/binding subresource this policy never
    matched. Pinning it at CREATE is the whole of what the rule ever enforced."""
    assert _nodename_rule(pod, is_job=is_job, operation=operation) is admitted


def test_bindings_deny_rather_than_warn(policy, bindings):
    """A policy with no binding, or one set to Warn, enforces nothing while looking applied."""
    assert bindings, "the policy has no binding at all — it would be inert"
    for b in bindings:
        assert b["spec"]["policyName"] == policy["metadata"]["name"]
        assert b["spec"]["validationActions"] == ["Deny"]
        assert b["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "ai-sandbox"
        }


def _binding_for(bindings, resource):
    matches = [
        b for b in bindings if any(r["resources"] == [resource] for r in b["spec"]["matchResources"]["resourceRules"])
    ]
    assert len(matches) == 1, f"expected exactly one binding covering {resource}, found {len(matches)}"
    return matches[0]


def test_the_job_pass_is_not_label_scoped(bindings):
    """A Job that simply omitted the label would otherwise skip the policy entirely —
    which is the bug this policy exists to catch."""
    assert "objectSelector" not in _binding_for(bindings, "jobs")["spec"]["matchResources"]


def test_the_pod_pass_selects_the_label_the_job_template_stamps(bindings):
    """Validating only the Job template leaves the window a mutating webhook writes into:
    the pod is created later, by the Job controller, from a template already passed. That
    second pass is label-scoped, so a label drift in job_manifest() would silently switch
    it off rather than fail anything — hence this test rather than a comment."""
    selector = _binding_for(bindings, "pods")["spec"]["matchResources"]["objectSelector"]["matchLabels"]
    template_labels = a_job()["spec"]["template"]["metadata"]["labels"]
    assert selector.items() <= template_labels.items(), (
        f"pod binding selects {selector}, but the Job template stamps {template_labels}"
    )


def test_policy_fails_closed_and_covers_both_kinds(policy):
    assert policy["spec"]["failurePolicy"] == "Fail"
    rules = {
        (tuple(r["apiGroups"]), tuple(r["resources"]), frozenset(r["operations"]))
        for r in policy["spec"]["matchConstraints"]["resourceRules"]
    }
    assert (("batch",), ("jobs",), frozenset({"CREATE", "UPDATE"})) in rules
    assert (("",), ("pods",), frozenset({"CREATE", "UPDATE"})) in rules


def test_the_job_pass_is_scoped_to_the_dispatchers_service_account(policy):
    """Scoping it to the one process holding create-Job RBAC is what lets the namespace
    also hold an ordinary maintenance Job (the JWKS refresher) without that Job having to
    satisfy a contract written for untrusted agent workloads.

    The Pod pass must stay unconditional: pods are created by the Job controller, so a
    blanket userInfo condition would switch the second pass off entirely."""
    conditions = policy["spec"]["matchConditions"]
    assert len(conditions) == 1
    expression = conditions[0]["expression"]
    assert "system:serviceaccount:ai-sandbox:sandbox-dispatcher" in expression
    assert "request.kind.kind != 'Job'" in expression, (
        "the condition must exempt non-Job requests, or it disables the Pod pass"
    )
    # Substrings cannot see the edit that matters here. Turn the `||` into `&&` and both
    # assertions above still hold, while the Pod pass — which no Pod request can satisfy
    # once it must ALSO come from the dispatcher's service account — is switched off with
    # this suite green. The digest below is what makes that edit fail; the two assertions
    # stay because they name what the condition is for when it does.
    assert {c["name"]: _digest(c["expression"]) for c in conditions} == MATCH_CONDITION_DIGESTS, (
        "the match condition that gates the whole policy changed — re-read this test's "
        "reasoning against the new text before re-pinning"
    )


# Every rule's expression, pinned by digest of its whitespace-normalised text.
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
    "task pods must not pin nodeName — it bypasses the scheduler, and with it the RuntimeClass node selector": "767387bc5943",
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
    "task containers must request exactly what they limit (Guaranteed QoS)": "e5824532099d",
    "task Jobs must set activeDeadlineSeconds, at most 3600 — an unbounded task holds a fleet node and bills for it": "3bf5faca144b",
    "task Jobs must run one pod at a time (parallelism: 1)": "240258b6ce47",
    "task Jobs must run exactly one pod (completions: 1)": "dd57deb1df82",
    "task Jobs must set backoffLimit: 0 — a retry re-clones and re-prompts the model, and bills for it": "80f672103226",
    "task Job pod templates must carry the app: sandbox-task label": "ab1151632d34",
}


# The expressions every pod-level rule is READ THROUGH, and the one that decides whether
# a request is evaluated at all. EXPRESSION_DIGESTS covers `validations` only, so before
# this a rewrite of `variables.pod` — the ternary that keeps the Job pass and the Pod pass
# looking at the same fields — silently changed the meaning of the twenty rules that read
# it, with nothing in this file able to see the edit.
MATCH_CONDITION_DIGESTS = {
    "job-writes-come-from-the-dispatcher": "7946d5ff4d30",
}
VARIABLE_DIGESTS = {
    "pod": "14b9d426b721",
    "isJob": "e99f115a3c1c",
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


def test_every_rule_has_a_distinct_message(policy):
    """Both MIRRORS and EXPRESSION_DIGESTS are keyed by message, so two rules sharing one
    would collapse into a single entry and quietly leave the other rule unguarded."""
    messages = [v["message"] for v in policy["spec"]["validations"]]
    duplicated = sorted({m for m in messages if messages.count(m) > 1})
    assert not duplicated, f"these messages are used by more than one rule: {duplicated}"


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


def test_an_unsubstituted_task_image_is_rejected(monkeypatch):
    """A last line rather than the first: the dispatcher already refuses at /run when
    AGENT_IMAGE is empty, and a deploy.sh that failed to substitute leaves the literal
    `__AGENT_IMAGE__` in dispatcher.yaml rather than an empty string. Both are rejected
    here, which is what the rule is for — neither is reachable past the checks in front
    of it today, and this is what keeps that true if one of them goes."""
    predicate, _ = next(
        mirror for message, mirror in MIRRORS.items() if message.startswith("task containers must run the task image")
    )
    for value in ("", "__AGENT_IMAGE__"):
        monkeypatch.setattr(kube, "AGENT_IMAGE", value)
        assert not predicate(a_job()), f"an image of {value!r} must not be admitted"


def test_the_policy_pins_the_repository_deploy_sh_actually_pushes_to(policy):
    """The third copy of the repository, and the one nothing else compares."""
    rule = next(
        v for v in policy["spec"]["validations"] if v["message"].startswith("task containers must run the task image")
    )
    assert f"'{IMAGE_REPOSITORY}:'" in rule["expression"], (
        f"the image rule pins {rule['expression']}, but deploy.sh pushes to {IMAGE_REPOSITORY} — "
        "every Job would be rejected"
    )
    assert rule["message"].endswith(IMAGE_REPOSITORY), "the rejection message names a repository nobody deploys"


def test_the_deadline_ceiling_admits_the_deadline_the_dispatcher_deploys(policy):
    """The 3600 in the policy is a second copy of TASK_DEADLINE_S, which is env
    configurable. Raise the env past the ceiling and every Job is rejected at dispatch —
    loudly, but with nothing in this suite noticing beforehand."""
    rule = next(v for v in policy["spec"]["validations"] if "activeDeadlineSeconds" in v["expression"])
    ceiling = int(re.search(r"activeDeadlineSeconds <= (\d+)", rule["expression"]).group(1))
    assert ceiling >= kube.TASK_DEADLINE_S, (
        f"the dispatcher's default deadline ({kube.TASK_DEADLINE_S}s) exceeds the policy ceiling ({ceiling}s)"
    )
    deployed = _deployed_env("TASK_DEADLINE_S")
    assert deployed is None or int(deployed) <= ceiling, (
        f"dispatcher.yaml deploys TASK_DEADLINE_S={deployed}, above the policy ceiling of {ceiling}s"
    )


def _deployed_env(name: str) -> str | None:
    """The literal value dispatcher.yaml sets for an env var, or None if it sets none.

    A value this test cannot read is not the same as no value, and returning None for
    both would quietly turn the comparison below into a no-op — so an `envFrom` block, or
    a `valueFrom` on this variable, fails here instead.
    """
    deployment = next(
        d
        for d in yaml.safe_load_all(DISPATCHER_MANIFEST.read_text())
        if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "sandbox-dispatcher"
    )
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        assert not container.get("envFrom"), (
            f"{container['name']} pulls env from an envFrom source, which could set {name} out of this test's sight"
        )
        for entry in container.get("env", []):
            if entry["name"] == name:
                assert "value" in entry, f"{name} is set from {entry.get('valueFrom')}, which this test cannot read"
                return entry["value"]
    return None


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
