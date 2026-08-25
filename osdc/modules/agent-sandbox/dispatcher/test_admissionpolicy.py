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

The mirrors are not a CEL evaluator and this file does not claim the expressions compile;
that is the API server's job on first apply. What it does claim is that the two
descriptions of a task pod have not diverged.
"""

from pathlib import Path

import dispatcher
import pytest
import yaml

POLICY_PATH = Path(__file__).resolve().parent.parent / "kubernetes" / "base" / "admissionpolicy.yaml"

# The tag deploy.sh substitutes is a content hash; only the repository is pinned.
GOOD_IMAGE = "harbor:30002/osdc/ci-agent-sandbox:0123456789ab"


@pytest.fixture(scope="module")
def documents():
    return list(yaml.safe_load_all(POLICY_PATH.read_text()))


@pytest.fixture(scope="module")
def policy(documents):
    return next(d for d in documents if d["kind"] == "ValidatingAdmissionPolicy")


@pytest.fixture(scope="module")
def bindings(documents):
    return [d for d in documents if d["kind"] == "ValidatingAdmissionPolicyBinding"]


def a_job(**pod_overrides) -> dict:
    """A real job_manifest() with the pod spec optionally mutated."""
    job = dispatcher.job_manifest("abc123456789", {"repo": "pytorch/pytorch", "task": "hello"})
    job["spec"]["template"]["spec"]["containers"][0]["image"] = GOOD_IMAGE
    job["spec"]["template"]["spec"].update(pod_overrides)
    return job


def _pod(job):
    return job["spec"]["template"]["spec"]


def _all_containers(job):
    return _pod(job)["containers"] + _pod(job).get("initContainers", [])


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
        lambda j: "nodeName" not in _pod(j),
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
        lambda j: all(c["image"].startswith("harbor:30002/osdc/ci-agent-sandbox:") for c in _all_containers(j)),
        lambda: a_job(containers=[{**a_job()["spec"]["template"]["spec"]["containers"][0], "image": "docker.io/alpine"}]),
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
        lambda: _with_container(securityContext={"privileged": True, "runAsNonRoot": True, "allowPrivilegeEscalation": False}),
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
        lambda j: all(c.get("securityContext", {}).get("procMount", "Default") == "Default" for c in _all_containers(j)),
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
    """What dispatcher.py builds today would be admitted."""
    predicate, _ = MIRRORS[message]
    assert predicate(a_job()), f"job_manifest() violates: {message}"


@pytest.mark.parametrize("message", sorted(MIRRORS))
def test_each_rule_rejects_its_own_violation(message):
    """A mirror that accepts everything passes the test above for the wrong reason."""
    predicate, violating = MIRRORS[message]
    assert not predicate(violating()), f"mirror does not actually enforce: {message}"


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
