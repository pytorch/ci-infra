"""The manifest loader, and the manifests actually checked in.

The checked-in manifests are loaded here with the same loader the dispatcher uses, so a
malformed one fails CI instead of crashlooping the dispatcher after a deploy.
"""

from __future__ import annotations

import copy
from pathlib import Path

import manifest
import pytest
import yaml
from manifest import ManifestError

MODULE = Path(__file__).resolve().parent.parent
CAPABILITIES = MODULE / "kubernetes" / "base" / "capabilities"

GOOD = {
    "name": "example",
    "owner": "pytorch-dev-infra",
    "clients": {
        "repos": [{"repository": "pytorch/ciforge", "repository_id": "1", "repository_owner_id": "2"}],
        "triggers": ["push"],
    },
    "model": {"id": "us.anthropic.claude-opus-5-5"},
    "sandbox": {"repos": ["pytorch/pytorch"]},
}


def mutated(path: str, value):
    """GOOD with the dotted `path` set to `value` (or deleted, for value=DELETE)."""
    document = copy.deepcopy(GOOD)
    *parents, leaf = path.split(".")
    node = document
    for key in parents:
        node = node[int(key)] if key.isdigit() else node[key]
    if value is DELETE:
        del node[leaf]
    else:
        node[leaf] = value
    return document


DELETE = object()


def test_a_good_manifest_parses():
    m = manifest.parse(GOOD, "example")
    assert m.name == "example"
    assert m.clients[0].repository_id == "1"
    assert m.triggers == {"push"}
    assert m.workflows == frozenset()
    assert m.model == "us.anthropic.claude-opus-5-5"
    assert m.sandbox_repos == ("pytorch/pytorch",)


def test_model_is_optional_and_empty_means_the_default():
    assert manifest.parse(mutated("model", DELETE), "example").model == ""
    assert manifest.parse(mutated("model", {"id": ""}), "example").model == ""


def test_workflows_are_parsed_when_present():
    m = manifest.parse(mutated("clients.workflows", [".github/workflows/review.yml"]), "example")
    assert m.workflows == {".github/workflows/review.yml"}


@pytest.mark.parametrize(
    ("path", "value", "why"),
    [
        ("surprise", "x", "unknown top-level key"),
        ("clients.surprise", "x", "unknown key in a region"),
        ("clients.repos.0.surprise", "x", "unknown key in a client repo"),
        ("model.surprise", "x", "unknown key in model"),
        ("sandbox.surprise", "x", "unknown key in sandbox"),
        ("name", "Example", "name not lowercase"),
        ("name", "other", "name does not match the file"),
        ("owner", "", "empty owner"),
        ("owner", DELETE, "missing owner"),
        ("clients", DELETE, "missing clients"),
        ("clients", [], "clients not a mapping"),
        ("clients.repos", [], "no client repos"),
        ("clients.repos", "pytorch/ciforge", "client repos not a list"),
        ("clients.repos.0.repository_id", 1, "integer id"),
        ("clients.repos.0.repository_id", "abc", "non-numeric id"),
        ("clients.repos.0.repository", "ciforge", "repository without an owner"),
        ("clients.repos.0.repository_owner_id", DELETE, "missing owner id"),
        ("clients.triggers", [], "no triggers"),
        ("clients.triggers", "push", "triggers not a list"),
        ("clients.triggers", ["push", "push"], "duplicate trigger"),
        ("clients.triggers", [1], "non-string trigger"),
        ("clients.workflows", ["review.yml"], "workflow outside .github/workflows"),
        ("model", {"id": 5}, "non-string model"),
        ("model", {}, "model without id"),
        ("sandbox", DELETE, "missing sandbox"),
        ("sandbox.repos", [], "no sandbox repos"),
        ("sandbox.repos", ["not a repo"], "malformed sandbox repo"),
    ],
)
def test_a_malformed_manifest_is_refused(path, value, why):
    with pytest.raises(ManifestError):
        manifest.parse(mutated(path, value), "example")


def test_effects_parse():
    document = copy.deepcopy(GOOD)
    document["capabilities"] = {
        "effects": [
            {"effect": "pr_comment", "max_bytes": 1000},
            {"effect": "check_run", "name": "ai-review", "conclusions": ["neutral", "success"]},
        ]
    }
    m = manifest.parse(document, "example")
    assert [e.kind for e in m.effects] == ["pr_comment", "check_run"]
    assert m.effects[0].max_bytes == 1000
    assert m.effects[1].conclusions == {"neutral", "success"}


def test_no_capabilities_means_read_only():
    assert manifest.parse(GOOD, "example").effects == ()


@pytest.mark.parametrize(
    "effects",
    [
        "pr_comment",
        [{"effect": "merge"}],
        [{"effect": "pr_comment", "name": "x"}],
        [{"effect": "pr_comment", "max_bytes": 0}],
        [{"effect": "pr_comment", "max_bytes": True}],
        [{"effect": "pr_comment", "max_bytes": 10**9}],
        [{"effect": "check_run", "name": "ai-review"}],
        [{"effect": "check_run", "conclusions": ["neutral"]}],
        [{"effect": "check_run", "name": "ai-review", "conclusions": ["action_required"]}],
        [{"effect": "check_run", "name": "bad\nname", "conclusions": ["neutral"]}],
        [{"effect": "pr_comment"}, {"effect": "pr_comment"}],
        [{"effect": "pr_comment", "surprise": 1}],
    ],
)
def test_malformed_effects_are_refused(effects):
    document = copy.deepcopy(GOOD)
    document["capabilities"] = {"effects": effects}
    with pytest.raises(ManifestError):
        manifest.parse(document, "example")


def test_a_trailing_newline_does_not_slip_past_a_pattern():
    with pytest.raises(ManifestError):
        manifest.parse(mutated("clients.repos.0.repository_id", "1\n"), "example")


def test_the_same_client_repo_twice_is_refused():
    document = copy.deepcopy(GOOD)
    document["clients"]["repos"].append(dict(document["clients"]["repos"][0]))
    with pytest.raises(ManifestError, match="listed twice"):
        manifest.parse(document, "example")


def test_a_non_mapping_document_is_refused():
    with pytest.raises(ManifestError, match="expected a mapping"):
        manifest.parse(["not", "a", "mapping"], "example")


def test_load_dir_keys_manifests_by_name(tmp_path):
    (tmp_path / "example.yaml").write_text(yaml.safe_dump(GOOD))
    (tmp_path / "notes.txt").write_text("ignored")
    assert list(manifest.load_dir(tmp_path)) == ["example"]


def test_an_empty_or_missing_directory_is_an_error_not_an_empty_policy(tmp_path):
    with pytest.raises(ManifestError, match="no capability manifests"):
        manifest.load_dir(tmp_path)
    with pytest.raises(ManifestError, match="no capability manifests"):
        manifest.load_dir(tmp_path / "absent")


def test_invalid_yaml_is_reported_with_the_file_name(tmp_path):
    (tmp_path / "broken.yaml").write_text("name: [unclosed")
    with pytest.raises(ManifestError, match=r"broken\.yaml"):
        manifest.load_dir(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        # The case that matters: a later key erasing an earlier restriction.
        "clients:\n  workflows: [.github/workflows/a.yaml]\n  workflows: []\n",
        "name: example\nname: other\n",
        "base: &b {triggers: [push]}\nclients:\n  <<: *b\n",
        'owner: !!str {=: "a", =: "b"}\n',
        'owner: !!str {=: "a", <<: {=: "b"}}\n',
    ],
    ids=["nested-restriction-erased", "top-level", "merge-key", "scalar-tagged-duplicate", "scalar-tagged-merge"],
)
def test_a_repeated_or_merged_key_is_refused_before_parsing(tmp_path, text):
    (tmp_path / "example.yaml").write_text(text)
    with pytest.raises(
        ManifestError, match=r"example\.yaml: not valid YAML.*(duplicate key|merge keys|stand in for a scalar)"
    ):
        manifest.load_dir(tmp_path)


def test_the_strict_loader_still_reads_a_normal_manifest(tmp_path):
    (tmp_path / "example.yaml").write_text(yaml.safe_dump(GOOD))
    assert manifest.load_dir(tmp_path)["example"] == manifest.parse(copy.deepcopy(GOOD), "example")


def test_every_checked_in_manifest_loads():
    loaded = manifest.load_dir(CAPABILITIES)
    assert loaded, "no manifests are checked in"


def test_every_checked_in_manifest_is_in_the_configmap():
    """kustomize's configMapGenerator takes an explicit file list. A manifest missing
    from it passes review, never reaches the cluster, and the caller it was written for
    is denied with no hint why."""
    kustomization = yaml.safe_load((MODULE / "kubernetes" / "base" / "kustomization.yaml").read_text())
    generators = {g["name"]: g for g in kustomization["configMapGenerator"]}
    listed = {Path(f).name for f in generators["agent-capabilities"]["files"]}
    on_disk = {p.name for p in CAPABILITIES.glob("*.yaml")}
    assert listed == on_disk


def test_the_dispatcher_mounts_the_manifests_where_the_loader_looks():
    documents = yaml.safe_load_all((MODULE / "kubernetes" / "base" / "dispatcher.yaml").read_text())
    deployment = next(d for d in documents if d and d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v.get("configMap", {}).get("name") == "agent-capabilities")
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == volume["name"])
    assert Path(mount["mountPath"]) == manifest.MANIFEST_DIR
