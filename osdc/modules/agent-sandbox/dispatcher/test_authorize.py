"""The authorization policy, as a table.

Two properties are being asserted here, and the second is the one that is easy to lose.

First, the obvious one: the allow/deny decisions are what we think they are.

Second, that the v2 seam is real. The same table runs against two policy sources — the v1
constants, and a stub loader carrying the signature a manifest loader will have. If a
future change makes any decision depend on where the values came from, the parametrised
run fails immediately rather than at the point someone tries to add the manifest. A seam
that is only promised in a design doc is not a seam.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import authorize
import pytest
import yaml
from authorize import Denied
from authorize import authorize as authorize_fn

MODULE = Path(__file__).resolve().parent.parent
DISPATCHER_MANIFEST = MODULE / "kubernetes" / "base" / "dispatcher.yaml"

# A token from the caller we do allow, with every claim the policy reads.
GOOD_CLAIMS = {
    "repository_id": "1133856973",
    "repository_owner_id": "21003710",
    "workflow_ref": "pytorch/ciforge/.github/workflows/ai-lint-run.yml@refs/heads/main",
    "job_workflow_ref": "pytorch/ciforge/.github/workflows/ai-lint-run.yml@refs/heads/main",
    "event_name": "workflow_run",
    # github-hosted, which no caller that can REACH /run actually reports — see
    # test_the_flag_is_off_while_no_admissible_caller_can_reach_run below.
    "runner_environment": "github-hosted",
    "ref_protected": "true",
}


def a_manifest_loader(caller):
    """Stands in for the v2 manifest loader: same signature, same return shape."""
    return (authorize.V1_CLONE_REPO, authorize.V1_MODEL)


# Both policy sources produce identical decisions, which is the claim being tested.
POLICIES = pytest.mark.parametrize("policy", [None, a_manifest_loader], ids=["v1-constants", "v2-loader-stub"])


def claims(**overrides):
    return {**GOOD_CLAIMS, **overrides}


@POLICIES
def test_the_allowed_caller_gets_a_grant(policy):
    grant = authorize_fn(claims(), {"task": "summarise the diff"}, policy)
    assert grant.caller == "pytorch/ciforge"
    assert grant.clone_repo == "pytorch/pytorch"
    assert grant.task == "summarise the diff"


@POLICIES
def test_the_grant_is_frozen(policy):
    """Nothing downstream may edit a decision after it is made."""
    grant = authorize_fn(claims(), {}, policy)
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.clone_repo = "attacker/repo"


@POLICIES
def test_the_request_cannot_choose_the_repository(policy):
    """The headline property. A caller naming another repo gets the policy's one, and
    http_api then refuses the request outright rather than silently substituting."""
    grant = authorize_fn(claims(), {"repo": "attacker/evil", "model": "some.expensive.model"}, policy)
    assert grant.clone_repo == "pytorch/pytorch"
    assert grant.model == authorize.V1_MODEL


@POLICIES
@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"repository_id": "999"}, "a different repo in the same org"),
        ({"repository_owner_id": "999"}, "the same repo name under a different owner"),
        ({"repository_id": None}, "no repository at all"),
    ],
    ids=["other-repo", "other-owner", "no-repo"],
)
def test_an_unlisted_caller_is_denied(policy, override, why):
    """Ids, not names: a repository can be renamed and its old name re-registered by
    somebody else, while repository_id cannot be moved."""
    with pytest.raises(Denied):
        authorize_fn(claims(**override), {}, policy)


@POLICIES
@pytest.mark.parametrize("event", sorted(authorize.DENIED_EVENTS))
def test_contributor_driven_events_are_denied(policy, event):
    with pytest.raises(Denied, match="not allowed to dispatch"):
        authorize_fn(claims(event_name=event), {}, policy)


@POLICIES
def test_a_missing_event_is_denied_rather_than_ignored(policy):
    with pytest.raises(Denied):
        authorize_fn(claims(event_name=None), {}, policy)


@POLICIES
def test_a_workflow_outside_the_allowed_repo_is_denied(policy):
    with pytest.raises(Denied, match="workflow is not on the allow-list"):
        authorize_fn(claims(workflow_ref="attacker/repo/.github/workflows/x.yml@refs/heads/main"), {}, policy)


@POLICIES
def test_a_reusable_workflow_from_elsewhere_is_denied(policy):
    """workflow_ref is the entry workflow, job_workflow_ref the file the job is defined
    in. Checking only the first lets an allowed repo delegate its identity to a workflow
    living anywhere."""
    with pytest.raises(Denied, match="job workflow"):
        authorize_fn(
            claims(job_workflow_ref="attacker/repo/.github/workflows/reusable.yml@refs/heads/main"), {}, policy
        )


@POLICIES
def test_an_absent_job_workflow_ref_is_denied(policy):
    """Required, not checked-if-present. A control that exists to stop an allowed repo
    delegating its identity is useless if suppressing one claim skips it. Corroborated
    against PyPI's Warehouse, which lists this claim as required and indexes it
    unguarded — GitHub emits it for ordinary jobs too, equal to workflow_ref."""
    without = {k: v for k, v in GOOD_CLAIMS.items() if k != "job_workflow_ref"}
    with pytest.raises(Denied, match="job workflow"):
        authorize_fn(without, {}, policy)


@POLICIES
def test_an_unexpected_runner_environment_is_denied(policy):
    with pytest.raises(Denied, match="runner environment"):
        authorize_fn(claims(runner_environment="self-hosted"), {}, policy)
    with pytest.raises(Denied, match="runner environment"):
        authorize_fn(claims(runner_environment=None), {}, policy)


def _ingress_namespaces() -> list[str]:
    """The namespaces `sandbox-agent-ingress` lets reach the dispatcher."""
    document = MODULE / "kubernetes" / "base" / "networkpolicy.yaml"
    ingress = next(
        d
        for d in yaml.safe_load_all(document.read_text())
        if d and d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "sandbox-agent-ingress"
    )
    return [
        peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for rule in ingress["spec"]["ingress"]
        for peer in rule["from"]
        if "namespaceSelector" in peer
    ]


def _deployed_require_auth() -> str:
    """The literal REQUIRE_AUTH the dispatcher Deployment ships."""
    deployment = next(
        d
        for d in yaml.safe_load_all(DISPATCHER_MANIFEST.read_text())
        if d and d["kind"] == "Deployment" and d["metadata"]["name"] == "sandbox-dispatcher"
    )
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        assert not container.get("envFrom"), f"{container['name']} could set REQUIRE_AUTH out of this test's sight"
        for entry in container.get("env", []):
            if entry["name"] == "REQUIRE_AUTH":
                assert "value" in entry, "REQUIRE_AUTH is set from a source this test cannot read"
                return entry["value"]
    raise AssertionError("dispatcher.yaml sets no REQUIRE_AUTH")


def test_the_flag_is_off_while_no_admissible_caller_can_reach_run():
    """Enforcement may not be enabled while the policy admits nobody who can connect.

    `/run` is a ClusterIP and `sandbox-agent-ingress` admits in-cluster namespaces only,
    so every caller that can reach it mints a SELF-HOSTED token — while the policy admits
    `github-hosted` alone. That disjointness is deliberate and fail-closed (see the
    comment on ALLOWED_RUNNER_ENVIRONMENTS), but it makes REQUIRE_AUTH=true a total
    outage rather than a hardening step, and the two files that would tell you so cannot
    see each other.

    So this does NOT assert the sets overlap — they do not, yet. It asserts the pair is
    consistent: widen the policy, or land the caller migration, BEFORE flipping the flag.
    """
    reachable_is_in_cluster = bool(_ingress_namespaces())
    admits_only_github_hosted = set(authorize.ALLOWED_RUNNER_ENVIRONMENTS) == {"github-hosted"}
    if reachable_is_in_cluster and admits_only_github_hosted:
        assert _deployed_require_auth() == "false", (
            f"REQUIRE_AUTH is enabled, but /run is reachable only from {_ingress_namespaces()} — in-cluster "
            "namespaces, whose runners mint self-hosted tokens — while the policy admits github-hosted only. "
            "Every request would be refused. See README 'Before enforcement can be enabled'."
        )


@POLICIES
def test_ref_protected_is_compared_as_a_string(policy):
    """GitHub sends "true", not true. An earlier build of this dispatcher would have
    denied 100% of production traffic on exactly this confusion while passing all of its
    unit tests, because two fixtures disagreed about the claim's type."""
    with pytest.raises(Denied, match="protected ref"):
        authorize_fn(claims(ref_protected=True), {}, policy)
    with pytest.raises(Denied, match="protected ref"):
        authorize_fn(claims(ref_protected="false"), {}, policy)
    assert authorize_fn(claims(ref_protected="true"), {}, policy).caller == "pytorch/ciforge"


@POLICIES
def test_a_non_string_task_is_denied(policy):
    with pytest.raises(Denied):
        authorize_fn(claims(), {"task": {"$ref": "something"}}, policy)


def test_the_allow_list_is_code_not_configuration():
    """It is the answer to "who may spend our Bedrock budget", so it belongs in git
    history and code review. An env var would move that decision out of both.

    Asserted against the source rather than by setting env vars and watching a request
    fail: the earlier version of this test set ALLOWED_CALLERS and ALLOWED_WORKFLOW_PREFIX
    and then submitted claims the module constants deny anyway, so it passed whether or
    not the module read the environment — it tested nothing. This fails the moment
    authorize.py grows a way to be configured from outside git.
    """
    source = Path(authorize.__file__).read_text()
    for reader in ("os.environ", "os.getenv", "getenv", "import os", "from os import", "open(", "read_text"):
        assert reader not in source, f"authorize.py references {reader!r} — the policy must stay a code constant"


def test_every_allowed_caller_carries_its_own_workflow_prefix():
    """The prefix is per caller, not global. A single global one would match a second
    entry on repository id and then deny it at the workflow check, which reads as a policy
    bug rather than as the misconfiguration it is."""
    names = [c["name"] for c in authorize.ALLOWED_CALLERS]
    assert len(names) == len(set(names)), (
        f"two allowed callers share a name, which is the /status ownership key: {names}"
    )
    for caller in authorize.ALLOWED_CALLERS:
        # Exactly `owner/repo/`, not merely starting with it: `pytorch/ciforge/nonexistent`
        # starts with the repo and still matches no workflow that repo can produce.
        assert caller["workflow_prefix"] == caller["name"] + "/", (
            f"{caller['name']} has workflow_prefix {caller['workflow_prefix']!r}; it must be exactly "
            f"{caller['name'] + '/'!r}, or it names a path no workflow of that repo can have"
        )


def test_an_unparseable_require_auth_value_crashes_rather_than_disabling_auth(monkeypatch):
    """`REQUIRE_AUTH=tru` under a `== "true"` comparison is a security control switched
    off by a typo, with no signal anywhere."""
    import http_api

    monkeypatch.setenv("REQUIRE_AUTH", "tru")
    with pytest.raises(RuntimeError, match="REQUIRE_AUTH"):
        http_api._flag("REQUIRE_AUTH", "false")
