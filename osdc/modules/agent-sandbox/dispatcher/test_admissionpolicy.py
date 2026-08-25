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
def binding(documents):
    return next(d for d in documents if d["kind"] == "ValidatingAdmissionPolicyBinding")


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
    "task Jobs must set runtimeClassName: gvisor (this is also what confines them to the ai-sandbox fleet)": (
        lambda j: _pod(j).get("runtimeClassName") == "gvisor",
        lambda: a_job(runtimeClassName="runc"),
    ),
    "task Jobs must run as serviceAccountName: sandbox-agent, which holds no RBAC": (
        lambda j: _pod(j).get("serviceAccountName") == "sandbox-agent",
        lambda: a_job(serviceAccountName="sandbox-dispatcher"),
    ),
    "task Jobs must set automountServiceAccountToken: false — a mounted token is API access from inside the sandbox": (
        lambda j: _pod(j).get("automountServiceAccountToken") is False,
        lambda: a_job(automountServiceAccountToken=True),
    ),
    "task Jobs must not share host namespaces (hostNetwork/hostPID/hostIPC)": (
        lambda j: not any(_pod(j).get(k) for k in ("hostNetwork", "hostPID", "hostIPC")),
        lambda: a_job(hostPID=True),
    ),
    "task Jobs must not pin nodeName — it bypasses the scheduler, and with it the RuntimeClass node selector": (
        lambda j: "nodeName" not in _pod(j),
        lambda: a_job(nodeName="ip-10-0-0-1"),
    ),
    "task Jobs must declare no volumes — see the module README before adding one": (
        lambda j: not _pod(j).get("volumes"),
        lambda: a_job(volumes=[{"name": "host", "hostPath": {"path": "/"}}]),
    ),
    "task containers must run the task image from harbor:30002/osdc/ci-agent-sandbox": (
        lambda j: all(c["image"].startswith("harbor:30002/osdc/ci-agent-sandbox:") for c in _all_containers(j)),
        lambda: a_job(containers=[{**a_job()["spec"]["template"]["spec"]["containers"][0], "image": "docker.io/alpine"}]),
    ),
    "a task Job runs exactly one container": (
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
    "task containers must set cpu, memory and ephemeral-storage limits": (
        lambda j: all(
            {"cpu", "memory", "ephemeral-storage"} <= set(c.get("resources", {}).get("limits", {}))
            for c in _all_containers(j)
        ),
        lambda: _with_container(resources={"requests": {"cpu": "2"}}),
    ),
    "task Jobs must set activeDeadlineSeconds, at most 3600 — an unbounded task holds a fleet node and bills for it": (
        lambda j: 0 < j["spec"].get("activeDeadlineSeconds", 0) <= 3600,
        lambda: _without_deadline(),
    ),
}


def _with_container(**container_overrides) -> dict:
    """A Job whose single container carries the given overrides."""
    job = a_job()
    _pod(job)["containers"][0].update(container_overrides)
    return job


def _without_deadline() -> dict:
    job = a_job()
    del job["spec"]["activeDeadlineSeconds"]
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


def test_binding_denies_rather_than_warns(policy, binding):
    """A policy with no binding, or a binding set to Warn, enforces nothing while looking applied."""
    assert binding["spec"]["policyName"] == policy["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["matchResources"]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "ai-sandbox"
    }


def test_policy_fails_closed_and_covers_job_writes(policy):
    assert policy["spec"]["failurePolicy"] == "Fail"
    rule = policy["spec"]["matchConstraints"]["resourceRules"][0]
    assert rule["apiGroups"] == ["batch"] and rule["resources"] == ["jobs"]
    assert set(rule["operations"]) == {"CREATE", "UPDATE"}
