"""The authorization policy, as a table, against the manifests actually checked in."""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import authorize
import manifest
import pytest
import yaml
from authorize import Denied
from authorize import authorize as authorize_fn

MODULE = Path(__file__).resolve().parent.parent
MANIFESTS = manifest.load_dir(MODULE / "kubernetes" / "base" / "capabilities")

# A token the ciforge-experiments manifest admits, with every claim the policy reads.
# Shaped after the real one minted by a ciforge job on the ue1 runners (2026-09-22).
GOOD_CLAIMS = {
    "repository": "pytorch/ciforge",
    "repository_id": "1133856973",
    "repository_owner_id": "21003710",
    "workflow_ref": "pytorch/ciforge/.github/workflows/agent-sandbox-oidc-probe.yml@refs/heads/iz2/oidc-probe",
    "job_workflow_ref": "pytorch/ciforge/.github/workflows/agent-sandbox-oidc-probe.yml@refs/heads/iz2/oidc-probe",
    "event_name": "workflow_dispatch",
    "runner_environment": "self-hosted",
    "ref_protected": "false",
}
GOOD_REQUEST = {"manifest": "ciforge-experiments", "task": "summarise the diff"}


def claims(**overrides):
    return {**GOOD_CLAIMS, **overrides}


def request(**overrides):
    return {**GOOD_REQUEST, **overrides}


def test_an_admitted_caller_gets_a_grant_from_its_manifest():
    grant = authorize_fn(claims(), request(), MANIFESTS)
    assert grant.caller == "pytorch/ciforge"
    assert grant.manifest == "ciforge-experiments"
    assert grant.clone_repo == "pytorch/pytorch"
    assert grant.model == MANIFESTS["ciforge-experiments"].model
    assert grant.task == "summarise the diff"


def test_an_unprotected_ref_is_not_a_reason_to_deny():
    """The manifest is pinned on the default branch, so the client can be untrusted
    (framework RFC). v1's ref_protected rule stood in for a manifest and is gone."""
    assert authorize_fn(claims(ref_protected="false"), request(), MANIFESTS).caller == "pytorch/ciforge"


def test_a_pr_triggered_client_is_admitted_where_its_manifest_allows_it():
    grant = authorize_fn(claims(event_name="pull_request"), request(manifest="ciforge-pr-review"), MANIFESTS)
    assert grant.model == "us.anthropic.claude-opus-5-5"


def test_the_grant_is_frozen():
    grant = authorize_fn(claims(), request(), MANIFESTS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        grant.clone_repo = "attacker/repo"


@pytest.mark.parametrize("name", [None, "", 5])
def test_a_request_without_a_manifest_is_denied(name):
    body = request()
    if name is None:
        del body["manifest"]
    else:
        body["manifest"] = name
    with pytest.raises(Denied, match="must name a capability manifest"):
        authorize_fn(claims(), body, MANIFESTS)


def test_an_unknown_manifest_is_denied():
    with pytest.raises(Denied, match="unknown capability manifest"):
        authorize_fn(claims(), request(manifest="nope"), MANIFESTS)


@pytest.mark.parametrize(
    "override",
    [{"repository_id": "999"}, {"repository_owner_id": "999"}, {"repository_id": None}],
    ids=["other-repo", "other-owner", "no-repo"],
)
def test_a_caller_the_manifest_does_not_list_is_denied(override):
    """Ids, not names: a repository can be renamed and its old name re-registered."""
    with pytest.raises(Denied, match="not a client"):
        authorize_fn(claims(**override), request(), MANIFESTS)


def test_the_right_repo_under_the_wrong_manifest_is_denied():
    with pytest.raises(Denied, match="not a client"):
        authorize_fn(claims(), request(manifest="osdc-integration-test"), MANIFESTS)


def test_an_event_the_manifest_does_not_list_is_denied():
    with pytest.raises(Denied, match="not a trigger"):
        authorize_fn(claims(event_name="pull_request"), request(), MANIFESTS)


def test_a_missing_event_is_denied_rather_than_ignored():
    with pytest.raises(Denied, match="no event_name"):
        authorize_fn(claims(event_name=None), request(), MANIFESTS)


def test_a_workflow_outside_the_client_repo_is_denied():
    with pytest.raises(Denied, match="workflow is not in the client repository"):
        authorize_fn(claims(workflow_ref="attacker/repo/.github/workflows/x.yml@refs/heads/main"), request(), MANIFESTS)


def test_a_reusable_workflow_from_elsewhere_is_denied():
    """Checking only workflow_ref lets an allowed repo delegate its identity to a
    workflow living anywhere."""
    with pytest.raises(Denied, match="job workflow"):
        authorize_fn(
            claims(job_workflow_ref="attacker/repo/.github/workflows/reusable.yml@refs/heads/main"),
            request(),
            MANIFESTS,
        )


def test_an_absent_job_workflow_ref_is_denied():
    without = {k: v for k, v in GOOD_CLAIMS.items() if k != "job_workflow_ref"}
    with pytest.raises(Denied, match="job workflow"):
        authorize_fn(without, request(), MANIFESTS)


def test_a_workflow_ref_without_a_ref_part_is_denied():
    with pytest.raises(Denied, match="workflow is not in the client repository"):
        authorize_fn(claims(workflow_ref="pytorch/ciforge/.github/workflows/x.yml"), request(), MANIFESTS)


def _canary_claims(workflow: str, job_workflow: str | None = None) -> dict:
    return claims(
        repository_id="398371105",
        workflow_ref=f"pytorch/pytorch-canary/{workflow}@refs/pull/1/merge",
        job_workflow_ref=f"pytorch/pytorch-canary/{job_workflow or workflow}@refs/pull/1/merge",
        event_name="pull_request",
    )


def test_a_listed_workflow_is_admitted():
    canary = _canary_claims(".github/workflows/integration-test.yaml")
    assert authorize_fn(canary, request(manifest="osdc-integration-test"), MANIFESTS).caller == "pytorch/pytorch-canary"


def test_an_unlisted_workflow_is_denied_when_the_manifest_lists_workflows():
    with pytest.raises(Denied, match="not listed"):
        authorize_fn(
            _canary_claims(".github/workflows/other.yml"), request(manifest="osdc-integration-test"), MANIFESTS
        )


def test_an_unlisted_reusable_workflow_is_denied_even_from_a_listed_entry():
    canary = _canary_claims(".github/workflows/integration-test.yaml", ".github/workflows/other.yml")
    with pytest.raises(Denied, match="not listed"):
        authorize_fn(canary, request(manifest="osdc-integration-test"), MANIFESTS)


def test_an_unexpected_runner_environment_is_denied():
    for value in ("github-hosted", None):
        with pytest.raises(Denied, match="runner environment"):
            authorize_fn(claims(runner_environment=value), request(), MANIFESTS)


def test_the_request_chooses_only_among_the_manifests_repositories():
    pr = claims(event_name="pull_request")
    grant = authorize_fn(pr, request(manifest="ciforge-pr-review", repo="pytorch/test-infra"), MANIFESTS)
    assert grant.clone_repo == "pytorch/test-infra"
    with pytest.raises(Denied, match="does not allow cloning"):
        authorize_fn(pr, request(manifest="ciforge-pr-review", repo="attacker/evil"), MANIFESTS)


def test_the_request_never_chooses_the_model():
    """authorize() never reads `model`; http_api refuses one that disagrees."""
    grant = authorize_fn(claims(), request(model="some.expensive.model"), MANIFESTS)
    assert grant.model == MANIFESTS["ciforge-experiments"].model


@pytest.mark.parametrize("field", ["task", "ref", "repo"])
def test_a_non_string_field_is_denied(field):
    with pytest.raises(Denied, match="must be strings"):
        authorize_fn(claims(), request(**{field: {"$ref": "x"}}), MANIFESTS)


def test_the_owner_key_carries_the_manifest():
    """Two manifests listing one repository must not share results through /status."""
    experiments = authorize_fn(claims(), request(), MANIFESTS)
    review = authorize_fn(claims(event_name="pull_request"), request(manifest="ciforge-pr-review"), MANIFESTS)
    assert experiments.caller == review.caller
    assert experiments.owner != review.owner


def test_a_branch_name_containing_an_at_sign_is_admitted():
    ref = "pytorch/ciforge/.github/workflows/probe.yml@refs/heads/feature@v2"
    assert authorize_fn(claims(workflow_ref=ref, job_workflow_ref=ref), request(), MANIFESTS).caller


def test_a_reusable_workflow_pinned_by_sha_is_admitted():
    job = "pytorch/ciforge/.github/workflows/reusable.yml@" + "a" * 40
    assert authorize_fn(claims(job_workflow_ref=job), request(), MANIFESTS).caller


def test_a_workflow_path_hiding_a_second_at_sign_is_denied():
    """Split at the first `@` and `listed.yaml@unlisted.yaml@refs/...` reads as listed."""
    sneaky = _canary_claims(".github/workflows/integration-test.yaml@unlisted.yaml")
    with pytest.raises(Denied, match="workflow is not in the client repository"):
        authorize_fn(sneaky, request(manifest="osdc-integration-test"), MANIFESTS)


def test_the_policy_is_data_in_git_not_process_configuration():
    """Manifests move the policy out of code, not out of review: authorize.py still
    reads nothing from its environment or the filesystem, and the manifests it is given
    come from a checked-in directory."""
    source = Path(authorize.__file__).read_text()
    for reader in ("os.environ", "os.getenv", "getenv", "import os", "from os import", "open(", "read_text"):
        assert reader not in source, f"authorize.py references {reader!r}"


def test_caller_keys_do_not_collide_across_repositories():
    """Grant.caller is half of the /status ownership key. Two client entries naming the
    same repository with different ids would share results."""
    by_name: dict[str, str] = {}
    for m in MANIFESTS.values():
        for client in m.clients:
            assert by_name.setdefault(client.repository, client.repository_id) == client.repository_id


def _ingress_namespaces() -> list[str]:
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


def test_an_admissible_caller_can_actually_reach_run():
    """The policy and the NetworkPolicy must describe an overlapping set of callers:
    /run is reachable only from in-cluster namespaces, whose runners mint self-hosted
    tokens."""
    assert _ingress_namespaces(), "sandbox-agent-ingress admits no namespace this test can read"
    assert "self-hosted" in authorize.ALLOWED_RUNNER_ENVIRONMENTS


def test_the_integration_test_is_admitted_by_its_manifest():
    """`test-agent-sandbox` runs in pytorch-canary on pull_request. If its manifest does
    not admit it, enabling enforcement breaks the one job that proves the sandbox works."""
    template = MODULE.parent.parent / "integration-tests" / "workflows" / "integration-test.yaml.tpl"
    assert "test-agent-sandbox:" in template.read_text()
    m = MANIFESTS["osdc-integration-test"]
    assert "pull_request" in m.triggers
    assert ".github/workflows/integration-test.yaml" in m.workflows


def test_manifests_are_not_shared_mutable_state():
    """authorize() must not edit what it is handed."""
    before = copy.deepcopy(MANIFESTS)
    authorize_fn(claims(), request(), MANIFESTS)
    assert before == MANIFESTS


def test_an_unparseable_require_auth_value_crashes_rather_than_disabling_auth(monkeypatch):
    import http_api

    monkeypatch.setenv("REQUIRE_AUTH", "tru")
    with pytest.raises(RuntimeError, match="REQUIRE_AUTH"):
        http_api._flag("REQUIRE_AUTH", "false")
