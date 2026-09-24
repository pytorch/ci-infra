"""Who may call, and what they get. Every authorization decision about a TOKEN is here.

The policy itself is data: capability manifests (manifest.py), one per use case, checked
in on ci-infra's default branch and deployed as a ConfigMap. The caller names a manifest;
this file checks the verified token against that manifest's `clients` region and builds a
Grant from the rest of it. Nothing about the caller's own branch is trusted — a manifest
edited in a pull request has no effect until it is merged and deployed, which is why the
client can be untrusted (framework RFC, "How a run works").

One decision is deliberately NOT here, and you have to read http_api.py for it: while
`REQUIRE_AUTH` is false, a request carrying no Authorization header at all is never shown
to this file — `http_api._grant_for` hands it the unauthenticated Grant directly. That is
the migration window, it is the only path that skips this file, and it disappears when
the flag flips.

JOB CONSTRUCTION consumes only the Grant. `kube.job_manifest` never sees the request
body; http_api reads `wait` to pick a response shape and compares a supplied `model`
against the Grant so a caller is told it was overruled, and neither reaches the Job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# What an unauthenticated caller gets during the migration window. Authenticated callers
# get their manifest's values instead.
V1_CLONE_REPO = "pytorch/pytorch"
V1_MODEL = ""  # empty means "the dispatcher's configured default"

# self-hosted, because `/run` is a ClusterIP that `sandbox-agent-ingress` opens to the
# `arc-runners` namespace only: every caller that can REACH it mints a self-hosted token.
# A SHAPE check ("the caller runs where the service is reachable from"), not a trust
# boundary: any job with `id-token: write` can mint a token on either kind of runner.
# Trust comes from the signature, the manifest match and the Grant. Goes away with the
# public endpoint.
ALLOWED_RUNNER_ENVIRONMENTS = frozenset({"self-hosted"})


class Denied(RuntimeError):
    """The caller is authenticated but not allowed to do this."""


@dataclass(frozen=True)
class Grant:
    """Everything the run is permitted to use, decided before any Job exists.

    Frozen, because the whole point is that nothing downstream may edit it. The Job
    builder takes one of these and never sees the request body.
    """

    # "pytorch/ciforge", for the log. The access-control key is `owner` below.
    caller: str
    # The manifest this run was granted under; empty for the unauthenticated path.
    manifest: str
    workflow_ref: str
    clone_repo: str
    model: str
    task: str
    # Caller-controlled: which branch, tag or commit of an allowed repository to read,
    # and optionally a base commit to diff it against. They select code to look at, not
    # a capability.
    ref: str
    base: str = ""
    # Writes the run may propose (manifest.EffectSpec). Empty for the unauthenticated
    # path and for read-only manifests.
    effects: tuple = ()

    @property
    def owner(self) -> str:
        """The task-ownership key /status compares against: caller AND manifest.

        Repository alone is not enough: a token one manifest admits (a pull_request run
        under ciforge-pr-review) must not read results produced under another
        (ciforge-experiments) just because both list the same repository.
        """
        return f"{self.caller}:{self.manifest}" if self.manifest else self.caller


# `.github/workflows/<file>@<git ref or commit sha>`. The file may not contain `@` or
# `/`, and the suffix must be a full ref or a sha, so neither `listed.yml@other.yml@refs/...`
# (read as listed.yml) nor a branch named `feature@v2` (split in the wrong place) confuses
# which file is being named.
_WORKFLOW_REF_RE = re.compile(r"(\.github/workflows/[^@/]+\.ya?ml)@(?:refs/.+|[0-9a-f]{40})")


def _workflow_path(ref: str, prefix: str) -> str | None:
    """`owner/repo/.github/workflows/x.yml@refs/heads/main` -> `.github/workflows/x.yml`,
    or None when the ref is not a workflow inside `prefix` (the client repository)."""
    if not ref.startswith(prefix):
        return None
    match = _WORKFLOW_REF_RE.fullmatch(ref[len(prefix) :])
    return match.group(1) if match else None


def admit(manifest, claims: dict):
    """The manifest's ClientRepo this verified token matches, or raise Denied.

    `claims` must already be signature-verified — this decides authorization, never
    authenticity.
    """
    client = next(
        (
            c
            for c in manifest.clients
            if claims.get("repository_id") == c.repository_id
            and claims.get("repository_owner_id") == c.repository_owner_id
        ),
        None,
    )
    if client is None:
        # Not echoing the claimed repository: naming which field failed is a probing oracle.
        raise Denied("caller is not a client of this manifest")

    event = claims.get("event_name")
    if not event:
        raise Denied("token carries no event_name")
    if event not in manifest.triggers:
        raise Denied(f"event {event} is not a trigger of manifest {manifest.name}")

    # workflow_ref is the entry workflow; job_workflow_ref is the file the job is defined
    # in, which differs when a reusable workflow is called. BOTH must be inside the client
    # repository, or an allowed repo could delegate its identity to a workflow living
    # anywhere. job_workflow_ref is REQUIRED: measured present on an ordinary push and
    # workflow_dispatch job (2026-09-22), and tolerating its absence would let a token
    # that suppresses the claim skip the control.
    prefix = f"{client.repository}/"
    entry = _workflow_path(claims.get("workflow_ref") or "", prefix)
    if entry is None:
        raise Denied("workflow is not in the client repository")
    job = _workflow_path(claims.get("job_workflow_ref") or "", prefix)
    if job is None:
        raise Denied("job workflow is not in the client repository")
    if manifest.workflows and not {entry, job} <= manifest.workflows:
        raise Denied(f"workflow is not listed in manifest {manifest.name}")

    if claims.get("runner_environment") not in ALLOWED_RUNNER_ENVIRONMENTS:
        raise Denied("runner environment is not allowed to dispatch agent tasks")
    return client


def authorize(claims: dict, request: dict, manifests: dict) -> Grant:
    """Turn verified OIDC claims plus a request into a Grant, or raise Denied."""
    name = request.get("manifest")
    if not isinstance(name, str) or not name:
        raise Denied("the request must name a capability manifest")
    manifest = manifests.get(name)
    if manifest is None:
        raise Denied(f"unknown capability manifest {name!r}")
    client = admit(manifest, claims)

    # The request contributes the prompt, the commit to read, and a CHOICE among the
    # repositories the manifest allows. It never names a model: http_api refuses a
    # supplied `model` that disagrees with the Grant.
    task = request.get("task", "")
    ref = request.get("ref", "")
    base = request.get("base", "")
    repo = request.get("repo", manifest.sandbox_repos[0])
    if not all(isinstance(v, str) for v in (task, ref, base, repo)):
        raise Denied("'task', 'ref', 'base' and 'repo' must be strings")
    if repo not in manifest.sandbox_repos:
        raise Denied(f"manifest {manifest.name} does not allow cloning {repo}")

    return Grant(
        caller=client.repository,
        manifest=manifest.name,
        workflow_ref=claims.get("workflow_ref") or "",
        clone_repo=repo,
        model=manifest.model,
        task=task,
        ref=ref,
        base=base,
        effects=manifest.effects,
    )
