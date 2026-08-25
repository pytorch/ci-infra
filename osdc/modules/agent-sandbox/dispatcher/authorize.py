"""Who may call, and what they get. The only file here that decides policy.

It imports nothing else in this process on purpose. A reviewer asking "what is a caller
allowed to make happen?" reads this file and stops.

The seam for v2 is a data type, not an interface. `authorize()` returns a frozen `Grant`
carrying every value the run is allowed to use — the model, the repository to clone —
and everything downstream builds the Job from the Grant alone. In v1 those values come
from the constants below. When the capability manifest lands, they come from the
manifest instead. What changes is where the values come from, never who decides them,
and that is why there is no policy interface here to implement: one implementation
behind a return type is just typed code, while one implementation behind an ABC is the
abstraction reviewers rightly object to.

Two rules keep that promise honest, and neither is enforced by a type:
  1. Nothing downstream of authorize() reads the request body. The Job is built from the
     Grant.
  2. Grant carries the fields v2 will govern even while v1 hardcodes them.
test_authorize.py runs the same allow/deny table against the v1 constants and against a
stub loader carrying the v2 signature, so the seam is exercised now rather than promised.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- v1 policy, as code -------------------------------------------------------------
#
# A module constant, not an env var: this is the answer to "who may spend our Bedrock
# budget", so it belongs in git history and in code review. Env vars stay for
# deploy-varying config like the audience and the namespace.
#
# pytorch/ciforge is the private repo we control and the sanctioned home for OIDC/auth
# experiments before they ship to pytorch/pytorch. The numeric ids are what is actually
# checked: a repository can be renamed and the name re-registered by someone else, while
# repository_id and repository_owner_id are immutable.
ALLOWED_CALLERS = (
    {
        "name": "pytorch/ciforge",
        "repository_id": "1133856973",
        "repository_owner_id": "21003710",  # the pytorch organisation
    },
)

# The workflow files inside an allowed repo whose tokens are accepted. A prefix, because
# GitHub spells these `owner/repo/.github/workflows/file.yml@refs/heads/main`.
ALLOWED_WORKFLOW_PREFIX = "pytorch/ciforge/"

# What an authorized run may do. v1 hardcodes both; v2 reads them from the manifest.
# The clone target is public, which is what lets the task pod clone with no credential
# at all — see the module README before changing it to a private repo.
V1_CLONE_REPO = "pytorch/pytorch"
V1_MODEL = ""  # empty means "the dispatcher's configured default"

# Events that carry contributor-controlled code or run before review. Denied outright in
# v1 rather than reasoned about: a pull_request token from a fork, if one can be minted
# at all, would be an authorized caller identity attached to an unreviewed workflow.
#
# workflow_run is deliberately NOT here, because it is the shape the real callers use —
# a stage that runs from the default branch after an untrusted stage finishes. See the
# module README on the residual that leaves: the prompt is caller-controlled, so a
# workflow that reads PR content can shape it. The Grant is what bounds the damage.
DENIED_EVENTS = frozenset({"pull_request", "pull_request_target", "pull_request_review", "issue_comment"})

# Verified 2026-08-25: every workflow in pytorch/ciforge runs on `ubuntu-latest`. Keeping
# this to github-hosted means a token minted on a self-hosted runner — including anything
# in the arc-runners fleet, which is the namespace that can already reach /run over the
# network — is not accepted.
ALLOWED_RUNNER_ENVIRONMENTS = frozenset({"github-hosted"})


class Denied(RuntimeError):
    """The caller is authenticated but not allowed to do this."""


@dataclass(frozen=True)
class Grant:
    """Everything the run is permitted to use, decided before any Job exists.

    Frozen, because the whole point is that nothing downstream may edit it. The Job
    builder takes one of these and never sees the request body.
    """

    caller: str  # "pytorch/ciforge" — for the audit line, not for any decision
    workflow_ref: str
    clone_repo: str
    model: str
    task: str
    # The one field that is caller-controlled and stays so: which commit of the
    # policy-pinned repository to read. It selects code to look at, not a capability —
    # the repository itself is not negotiable, and neither is the model.
    ref: str


def _lookup_caller(claims: dict) -> dict | None:
    for allowed in ALLOWED_CALLERS:
        if (
            claims.get("repository_id") == allowed["repository_id"]
            and claims.get("repository_owner_id") == allowed["repository_owner_id"]
        ):
            return allowed
    return None


def authorize(claims: dict, request: dict, policy=None) -> Grant:
    """Turn verified OIDC claims plus a request into a Grant, or raise Denied.

    `claims` must already be signature-verified — this function decides authorization,
    never authenticity. `policy` is the v2 seam: a callable taking the matched caller and
    returning the clone repo and model. v1 passes None and uses the constants above.
    """
    caller = _lookup_caller(claims)
    if caller is None:
        # Deliberately not echoing the claimed repository back: the message goes to an
        # unauthorized caller, and naming which field failed is a probing oracle.
        raise Denied("caller is not on the allow-list")

    event = claims.get("event_name")
    if not event:
        raise Denied("token carries no event_name")
    if event in DENIED_EVENTS:
        raise Denied(f"event {event} is not allowed to dispatch agent tasks")

    # workflow_ref is the entry workflow; job_workflow_ref is the workflow file the job is
    # actually defined in, which differs when a reusable workflow is called. BOTH must be
    # inside the allowed repo, or an allowed repo could delegate its identity to a
    # workflow living anywhere.
    #
    # job_workflow_ref is REQUIRED, not checked-if-present. An earlier draft tolerated its
    # absence because we had not confirmed GitHub emits it for an ordinary non-reusable
    # job — but "tolerate when absent" on a control whose whole job is to stop delegation
    # means an attacker who can suppress the claim skips the control. Corroboration that
    # it is always emitted: PyPI's Warehouse, a production GitHub OIDC verifier, lists it
    # in __required_verifiable_claims__ and indexes it unguarded
    # (warehouse/oidc/models/github.py). If that turns out to be wrong the failure is a
    # clear 403 naming the claim, which is the direction to be wrong in.
    workflow_ref = claims.get("workflow_ref") or ""
    if not workflow_ref.startswith(ALLOWED_WORKFLOW_PREFIX):
        raise Denied("workflow is not on the allow-list")
    job_workflow_ref = claims.get("job_workflow_ref") or ""
    if not job_workflow_ref.startswith(ALLOWED_WORKFLOW_PREFIX):
        raise Denied("job workflow is not on the allow-list")

    if claims.get("runner_environment") not in ALLOWED_RUNNER_ENVIRONMENTS:
        raise Denied("runner environment is not allowed to dispatch agent tasks")

    # A STRING, not a boolean. GitHub sends ref_protected as "true"/"false", and a
    # manifest or fixture that writes a YAML boolean here compares unequal to every real
    # token — an earlier build of this dispatcher would have denied 100% of production
    # traffic while passing all of its unit tests, because two fixtures disagreed about
    # this claim's type and each suite validated its own belief. Assert the string.
    if claims.get("ref_protected") != "true":
        raise Denied("only a protected ref may dispatch agent tasks")

    clone_repo, model = policy(caller) if policy else (V1_CLONE_REPO, V1_MODEL)

    # The request contributes the prompt and the commit to read. Everything else about
    # the run is policy: a caller cannot name a repository to clone or a model to spend,
    # and `repo` in the body is ignored rather than validated, so there is no version of
    # this where a validation slip lets one through.
    task = request.get("task", "")
    ref = request.get("ref", "")
    if not isinstance(task, str) or not isinstance(ref, str):
        raise Denied("'task' and 'ref' must be strings")

    return Grant(
        caller=caller["name"],
        workflow_ref=workflow_ref,
        clone_repo=clone_repo,
        model=model,
        task=task,
        ref=ref,
    )
