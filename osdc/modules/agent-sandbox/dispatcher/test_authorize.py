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

import authorize
import pytest
from authorize import Denied, authorize as authorize_fn

# A token from the caller we do allow, with every claim the policy reads.
GOOD_CLAIMS = {
    "repository_id": "1133856973",
    "repository_owner_id": "21003710",
    "workflow_ref": "pytorch/ciforge/.github/workflows/ai-lint-run.yml@refs/heads/main",
    "job_workflow_ref": "pytorch/ciforge/.github/workflows/ai-lint-run.yml@refs/heads/main",
    "event_name": "workflow_run",
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
    with pytest.raises(Exception):  # noqa: B017 — dataclasses raises FrozenInstanceError
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
def test_a_self_hosted_runner_is_denied(policy):
    """arc-runners is the namespace that can already reach /run over the network. A token
    minted there must not also be accepted."""
    with pytest.raises(Denied, match="runner environment"):
        authorize_fn(claims(runner_environment="self-hosted"), {}, policy)


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


def test_the_allow_list_is_code_not_configuration(monkeypatch):
    """It is the answer to "who may spend our Bedrock budget", so it belongs in git
    history and code review. An env var would move that decision out of both."""
    monkeypatch.setenv("ALLOWED_CALLERS", "attacker/repo")
    monkeypatch.setenv("ALLOWED_WORKFLOW_PREFIX", "attacker/repo/")
    with pytest.raises(Denied):
        authorize_fn(claims(repository_id="999"), {}, None)


def test_an_unparseable_require_auth_value_crashes_rather_than_disabling_auth(monkeypatch):
    """`REQUIRE_AUTH=tru` under a `== "true"` comparison is a security control switched
    off by a typo, with no signal anywhere."""
    import http_api

    monkeypatch.setenv("REQUIRE_AUTH", "tru")
    with pytest.raises(RuntimeError, match="REQUIRE_AUTH"):
        http_api._flag("REQUIRE_AUTH", "false")
