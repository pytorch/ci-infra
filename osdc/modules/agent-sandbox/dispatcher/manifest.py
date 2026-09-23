"""Capability manifests: who may call, and what the run they get may use.

One YAML file per manifest under kubernetes/base/capabilities/, rendered into a
ConfigMap at deploy time and mounted read-only here. A manifest therefore changes only
through a reviewed commit on ci-infra's default branch followed by a deploy, which is
what the framework RFC means by "pinned on the default branch": a caller cannot edit the
manifest it is judged against, so the client itself can be untrusted.

This module only PARSES. Matching a token against a manifest is an authorization
decision and lives in authorize.py with the others.

Strict on purpose: an unknown key anywhere is an error, and so is a key repeated within
one mapping (plain YAML keeps the last value, so a later `workflows: []` would silently
erase an earlier restriction). A mistyped constraint fails the deploy instead of silently
granting more than its author meant. Regions follow the RFC's names (name, owner,
clients, model, sandbox, capabilities); the rest of the RFC schema arrives with the
features that need it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

MANIFEST_DIR = Path(os.environ.get("MANIFEST_DIR", "/etc/agent-sandbox/capabilities"))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ID_RE = re.compile(r"^[0-9]+$")
WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
CHECK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./-]{0,99}$")
CHECK_CONCLUSIONS = frozenset({"success", "failure", "neutral"})
# GitHub's own limits: a comment body and a check-run summary are both capped near 64 KiB.
MAX_EFFECT_BYTES = 65536


class ManifestError(ValueError):
    """A manifest is malformed or carries a key the schema does not define."""


@dataclass(frozen=True)
class ClientRepo:
    repository: str
    # Strings, because that is how GitHub spells them in a token. A YAML integer here
    # would compare unequal to every real claim and deny every call.
    repository_id: str
    repository_owner_id: str


@dataclass(frozen=True)
class EffectSpec:
    """One write the run may PROPOSE. The agent never writes: it proposes, the dispatcher
    checks the proposal against this, and a separate applier performs it."""

    kind: str  # "pr_comment" or "check_run"
    max_bytes: int = MAX_EFFECT_BYTES
    # check_run only: the name is fixed by the manifest, the conclusion chosen from a set.
    name: str = ""
    conclusions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Manifest:
    name: str
    owner: str
    clients: tuple[ClientRepo, ...]
    triggers: frozenset[str]
    # Workflow files, relative to the client repo, allowed to call. Empty means any
    # workflow in a listed repo.
    workflows: frozenset[str]
    # Bedrock model id; empty means the dispatcher's configured default.
    model: str
    # Public repositories the task may clone. The first is the default.
    sandbox_repos: tuple[str, ...]
    # Writes the run may propose. Empty: a read-only manifest, the default.
    effects: tuple[EffectSpec, ...] = ()


def _mapping(value, where: str, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: expected a mapping, got {type(value).__name__}")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ManifestError(f"{where}: unknown key(s) {unknown}")
    missing = sorted(required - set(value))
    if missing:
        raise ManifestError(f"{where}: missing key(s) {missing}")
    return value


def _string(value, where: str, pattern: re.Pattern | None = None, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{where}: expected a string, got {type(value).__name__}")
    if not value and not allow_empty:
        raise ManifestError(f"{where}: must not be empty")
    if value and pattern is not None and not pattern.fullmatch(value):
        raise ManifestError(f"{where}: {value!r} does not match {pattern.pattern}")
    return value


def _string_list(value, where: str, pattern: re.Pattern | None = None, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ManifestError(f"{where}: expected a list, got {type(value).__name__}")
    if not value and not allow_empty:
        raise ManifestError(f"{where}: must not be empty (default-deny)")
    items = [_string(v, f"{where}[{i}]", pattern) for i, v in enumerate(value)]
    if len(set(items)) != len(items):
        raise ManifestError(f"{where}: duplicate entries")
    return items


def _client_repo(entry, where: str) -> ClientRepo:
    keys = {"repository", "repository_id", "repository_owner_id"}
    entry = _mapping(entry, where, keys, keys)
    return ClientRepo(
        repository=_string(entry["repository"], f"{where}.repository", REPO_RE),
        repository_id=_string(entry["repository_id"], f"{where}.repository_id", ID_RE),
        repository_owner_id=_string(entry["repository_owner_id"], f"{where}.repository_owner_id", ID_RE),
    )


def _effect(entry, where: str) -> EffectSpec:
    entry = _mapping(entry, where, {"effect", "max_bytes", "name", "conclusions"}, {"effect"})
    kind = _string(entry["effect"], f"{where}.effect")
    max_bytes = entry.get("max_bytes", MAX_EFFECT_BYTES)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_EFFECT_BYTES:
        raise ManifestError(f"{where}.max_bytes: must be an integer in 1..{MAX_EFFECT_BYTES}")
    if kind == "pr_comment":
        if set(entry) - {"effect", "max_bytes"}:
            raise ManifestError(f"{where}: pr_comment takes only effect and max_bytes")
        return EffectSpec(kind=kind, max_bytes=max_bytes)
    if kind == "check_run":
        _mapping(entry, where, {"effect", "max_bytes", "name", "conclusions"}, {"effect", "name", "conclusions"})
        conclusions = _string_list(entry["conclusions"], f"{where}.conclusions")
        if not set(conclusions) <= CHECK_CONCLUSIONS:
            raise ManifestError(f"{where}.conclusions: allowed values are {sorted(CHECK_CONCLUSIONS)}")
        return EffectSpec(
            kind=kind,
            max_bytes=max_bytes,
            name=_string(entry["name"], f"{where}.name", CHECK_NAME_RE),
            conclusions=frozenset(conclusions),
        )
    raise ManifestError(f"{where}.effect: unknown effect {kind!r}; known are pr_comment, check_run")


def parse(document, expected_name: str) -> Manifest:
    """Validate one parsed YAML document. `expected_name` is the file's stem."""
    top = _mapping(
        document,
        expected_name,
        {"name", "owner", "clients", "model", "sandbox", "capabilities"},
        {"name", "owner", "clients", "sandbox"},
    )
    name = _string(top["name"], "name", NAME_RE)
    if name != expected_name:
        raise ManifestError(f"name {name!r} must match its file name {expected_name!r}")

    clients = _mapping(top["clients"], f"{name}.clients", {"repos", "triggers", "workflows"}, {"repos", "triggers"})
    if not isinstance(clients["repos"], list) or not clients["repos"]:
        raise ManifestError(f"{name}.clients.repos: must be a non-empty list (default-deny)")
    repos = tuple(_client_repo(e, f"{name}.clients.repos[{i}]") for i, e in enumerate(clients["repos"]))
    if len({r.repository_id for r in repos}) != len(repos):
        raise ManifestError(f"{name}.clients.repos: a repository is listed twice")

    model = ""
    if "model" in top:
        model_region = _mapping(top["model"], f"{name}.model", {"id"}, {"id"})
        model = _string(model_region["id"], f"{name}.model.id", allow_empty=True)

    sandbox = _mapping(top["sandbox"], f"{name}.sandbox", {"repos"}, {"repos"})

    effects: tuple[EffectSpec, ...] = ()
    if "capabilities" in top:
        caps = _mapping(top["capabilities"], f"{name}.capabilities", {"effects"}, {"effects"})
        if not isinstance(caps["effects"], list):
            raise ManifestError(f"{name}.capabilities.effects: expected a list")
        effects = tuple(_effect(e, f"{name}.capabilities.effects[{i}]") for i, e in enumerate(caps["effects"]))
        if len({e.kind for e in effects}) != len(effects):
            raise ManifestError(f"{name}.capabilities.effects: an effect is listed twice")

    return Manifest(
        name=name,
        owner=_string(top["owner"], f"{name}.owner"),
        clients=repos,
        triggers=frozenset(_string_list(clients["triggers"], f"{name}.clients.triggers")),
        workflows=frozenset(
            _string_list(clients.get("workflows", []), f"{name}.clients.workflows", WORKFLOW_RE, allow_empty=True)
        ),
        model=model,
        sandbox_repos=tuple(_string_list(sandbox["repos"], f"{name}.sandbox.repos", REPO_RE)),
        effects=effects,
    )


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated key and YAML merge keys (`<<`).

    Merge keys are refused rather than supported because a merge is exactly an override
    that the reader of the file does not see; a manifest is short enough to spell out.
    So is a mapping read as a scalar (`!!str {=: "1", =: "2"}`): SafeLoader takes its `=`
    value without ever calling construct_mapping, which would skip both checks.
    """

    def construct_scalar(self, node):
        if isinstance(node, yaml.MappingNode):
            raise yaml.constructor.ConstructorError(
                None, None, "a mapping cannot stand in for a scalar", node.start_mark
            )
        return super().construct_scalar(node)

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise yaml.constructor.ConstructorError(
                        None, None, "merge keys (<<) are not allowed", key_node.start_mark
                    )
                key = self.construct_object(key_node, deep=True)
                try:
                    duplicate = key in seen
                except TypeError:  # unhashable: SafeLoader itself reports it below
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_dir(directory: Path) -> dict[str, Manifest]:
    """Every *.yaml in `directory`, keyed by name. Raises ManifestError on any problem.

    An empty or missing directory is an error rather than an empty policy: the dispatcher
    would otherwise start, deny every authenticated call, and look healthy doing it.
    """
    files = sorted(directory.glob("*.yaml")) if directory.is_dir() else []
    if not files:
        raise ManifestError(f"no capability manifests found in {directory}")
    manifests = {}
    for path in files:
        try:
            document = yaml.load(path.read_text(), Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
        except yaml.YAMLError as exc:
            raise ManifestError(f"{path.name}: not valid YAML: {exc}") from None
        manifests[path.stem] = parse(document, path.stem)
    return manifests
