"""Effect policy: which of the writes a task proposed may leave the dispatcher.

The agent never writes. It PROPOSES effects (a PR comment, a check run), the task prints
them in its result, and this module keeps only those the run's Grant allows, in the shape
the manifest fixes, stamped with the commit the agent actually read. An applier outside
the sandbox performs what survives, after re-checking that the pull request still points
at that commit.

Everything the task returns is untrusted: the model read repository content, which may
carry instructions. So nothing here is taken on trust — kind, size, conclusion and name
are all re-checked, and the check-run name comes from the manifest, never the proposal.
"""

from __future__ import annotations

import re

SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_PROPOSALS = 3
MAX_TITLE_CHARS = 200


def attribution(head: str, manifest: str) -> str:
    """Put IN FRONT of every body by the dispatcher, inside the size limit, so every write
    says where it came from and which commit it is about. In front, because untrusted
    Markdown after it cannot hide it: a body ending in `<!--` would swallow a footer."""
    return f"<sub>agent-sandbox · {manifest} · commit {head[:12]}</sub>\n\n"


def _text(value, limit_bytes: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit_bytes:
        return None
    return value


def screen(result: dict, grant) -> dict:
    """Replace result["effects"] with the proposals the Grant allows; say why the rest
    were dropped in result["errors"]["effects"]. Returns the same dict. Never raises: a
    malformed result must not escape the task lifecycle and strand its slot.

    The commit comes from the GRANT, never from the task: effects are allowed only when
    the caller asked for a commit sha, and only when the task reports having checked out
    exactly that commit. A task claiming another commit gets nothing.
    """
    try:
        return _screen(result, grant)
    except Exception as exc:
        result.pop("effects", None)
        result["errors"] = result.get("errors") if isinstance(result.get("errors"), dict) else {}
        result["errors"]["effects"] = f"effects dropped: {type(exc).__name__}"
        result["effects"] = []
        return result


def _screen(result: dict, grant) -> dict:
    proposals = result.pop("effects", None)
    if proposals is None:
        return result
    errors = result.get("errors")
    if not isinstance(errors, dict):
        # Malformed errors are still errors; keep them, under a key of their own.
        result["errors"] = {"result": errors} if errors else {}
    allowed = {e.kind: e for e in grant.effects}
    pinned = grant.ref if SHA_RE.fullmatch(grant.ref or "") else ""
    accepted, rejected = [], []
    if not isinstance(proposals, list):
        rejected.append("effects must be a list")
        proposals = []
    if proposals and any(key != "effects" for key in result["errors"]):
        # Only a clean run writes: an agent, model, clone or dispatch error means the
        # proposals were made on an incomplete picture.
        rejected.append("the run reported errors, so its proposals are dropped")
        proposals = []
    elif proposals and not pinned:
        rejected.append("effects need the request's ref to be a full commit sha")
        proposals = []
    elif proposals and result.get("head_sha") != pinned:
        rejected.append("the task did not report checking out the requested commit")
        proposals = []
    for i, proposal in enumerate(proposals):
        if len(accepted) >= MAX_PROPOSALS:
            rejected.append(f"#{i}: more than {MAX_PROPOSALS} effects")
            continue
        if not isinstance(proposal, dict) or not isinstance(proposal.get("effect"), str):
            rejected.append(f"#{i}: not an effect object")
            continue
        spec = allowed.get(proposal["effect"])
        if spec is None:
            rejected.append(
                f"#{i}: effect {proposal['effect'][:40]!r} is not allowed by manifest {grant.manifest or '(none)'}"
            )
            continue
        head = attribution(pinned, grant.manifest)
        room = spec.max_bytes - len(head.encode())
        body = _text(proposal.get("body"), room)
        if body is None:
            rejected.append(f"#{i}: body must be non-empty text of at most {room} bytes")
            continue
        effect = {"effect": spec.kind, "repo": grant.clone_repo, "head_sha": pinned, "body": head + body}
        if spec.kind == "check_run":
            conclusion = proposal.get("conclusion")
            title = proposal.get("title", spec.name)
            if not isinstance(conclusion, str) or conclusion not in spec.conclusions:
                rejected.append(f"#{i}: conclusion must be one of {sorted(spec.conclusions)}")
                continue
            if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARS:
                rejected.append(f"#{i}: title must be non-empty text of at most {MAX_TITLE_CHARS} characters")
                continue
            effect.update(name=spec.name, conclusion=conclusion, title=title)
        accepted.append(effect)
    result["effects"] = accepted
    if rejected:
        result["errors"]["effects"] = "; ".join(rejected)[:4000]
    return result
