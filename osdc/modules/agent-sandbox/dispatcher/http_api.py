"""The HTTP surface: parse, bound, dispatch, answer. No task logic lives here.

Endpoints:
  GET  /healthz      -> {"status": "ok"}
  POST /run          -> body {"manifest"?,"repo"?,"ref"?,"base"?,"task"?,"wait"?}
                        `ref` is a branch, tag or commit sha; `base` an optional commit sha
                        whose diff against `ref` the task is given.
                        `manifest` names the capability manifest; required with a token.
                        wait=true (default): blocks, returns the task result
                        wait=false: returns {"task_id": ...} immediately
                        `repo` picks among the repositories the manifest allows; `model`
                        is still ACCEPTED but is policy, not request, and a supplied value
                        that disagrees with the Grant is a 403 rather than a substitution.
  GET  /status/<id>[?manifest=<name>]
                     -> {"state": "running"|"done", ...result}, scoped to the caller and
                        manifest that own the task; anyone else's reads as 404. A token
                        caller names the manifest it ran under.

Everything below the parse is somebody else's file, so the rule for this one is that it
owns request shape, status codes and response bodies — no task state, and no Kubernetes
call of its own. The one value it reads out of another module is kube.AGENT_IMAGE, so that
a deploy which left the task image empty answers 500 rather than creating a Job.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import authorize
import kube
import manifest
import oidc
import tasks

# Request-surface bounds. The body is read under a size cap and the connection under a
# socket timeout, before and independently of any authentication.
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 30


def _flag(name: str, default: str) -> bool:
    """A boolean env var that refuses to guess.

    `REQUIRE_AUTH=tru` under a `== "true"` comparison is silently False, which is a
    security control switched off by a typo. Unrecognised values abort at import instead:
    the pod crashloops with the reason, which is loud and safe, rather than serving
    traffic with authentication quietly disabled.
    """
    raw = os.environ.get(name, default).strip().lower()
    if raw not in ("true", "false"):
        raise RuntimeError(f"{name} must be 'true' or 'false', got {raw!r}")
    return raw == "true"


# Whether a request carrying NO Authorization header at all is refused.
#
# The flag governs exactly one case: the caller that sends nothing. A token that IS
# presented is always verified and authorized whatever this says, so a forged or denied
# token is rejected either way.
#
# BE CLEAR ABOUT WHAT THAT DOES AND DOES NOT BUY. While this is false, authentication is
# optional, and an unauthenticated caller can therefore do MORE than a caller whose real
# token was denied — it simply omits the header. This is a migration window, not a
# security posture, and it is only tolerable because /run is already reachable
# unauthenticated by the whole arc-runners namespace today; it is strictly not worse than
# the status quo, and strictly better once flipped.
#
# It ships false because today's caller sends no token. The Deployment must flip it once
# the GHA client does. See the module README.
REQUIRE_AUTH = _flag("REQUIRE_AUTH", "false")

TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
SHA_RE = re.compile(r"[0-9a-f]{40}")


# `ref` and `base` become git arguments in the task pod, which re-checks them with the
# same rules (agent/sandbox.py). Kept in step by test_ref_rules_match_the_task_pod.
def valid_ref(name: str) -> bool:
    """A branch or tag name git would accept (`git check-ref-format --branch` rules), or a
    full commit sha. It becomes a git argument, so it may not start with `-`."""
    if not isinstance(name, str) or not 0 < len(name) <= 1024:
        return False
    if SHA_RE.fullmatch(name):
        return True
    if name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c in " ~^:?*[\\" for c in name):
        return False
    if ".." in name or "//" in name or "@{" in name or name == "@":
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in name.split("/"))


_MANIFESTS: dict | None = None


def manifests() -> dict:
    """The capability manifests, loaded once. __main__ calls this at startup so a bad
    or missing manifest crashloops the pod instead of failing the first request."""
    global _MANIFESTS
    if _MANIFESTS is None:
        _MANIFESTS = manifest.load_dir(manifest.MANIFEST_DIR)
    return _MANIFESTS


class Handler(BaseHTTPRequestHandler):
    # Whole-connection socket timeout (socketserver applies it in setup()). Without it
    # a caller that announces a body and sends it slowly, or never, parks a handler
    # thread for as long as it likes, and ThreadingHTTPServer caps nothing. A waiting
    # /run sits between socket operations, so it never trips this.
    timeout = REQUEST_TIMEOUT_S

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            # in_flight/capacity are here because a 429 is otherwise indistinguishable
            # from a wedged dispatcher: this says which one it is without kubectl.
            self._send(
                200,
                {"status": "ok", "in_flight": tasks.slots_in_use(), "capacity": tasks.MAX_CONCURRENT_TASKS},
            )
            return
        if self.path.startswith("/status/"):
            url = urllib.parse.urlsplit(self.path)
            task_id = url.path[len("/status/") :]
            if not TASK_ID_RE.match(task_id):
                self._send(400, {"error": "malformed task id"})
                return
            query = urllib.parse.parse_qs(url.query)
            try:
                caller = self._owner(query.get("manifest", [""])[0])
            except oidc.InvalidToken as exc:
                self._send(401, {"error": str(exc)})
                return
            except authorize.Denied as exc:
                self._send(403, {"error": str(exc)})
                return
            # A task belonging to someone else is 404, not 403 — see tasks.status().
            payload = tasks.status(task_id, caller)
            if payload is None:
                self._send(404, {"error": "unknown task id"})
                return
            self._send(200, payload)
            return
        self._send(404, {"error": "not found"})

    def _read_spec(self) -> dict:
        """Parse and validate the /run body, raising ValueError with the reason.

        Bounded, because the declared length is caller-controlled and read(-1) would
        read until end of file. Types are checked rather than truth-tested: a field of
        the wrong type reaches git or the proxy as an argument.
        """
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError(f"invalid Content-Length: {raw_length!r}") from None
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError(f"Content-Length must be between 0 and {MAX_BODY_BYTES}, got {length}")

        spec = json.loads(self.rfile.read(length) or "{}")
        if not isinstance(spec, dict):
            raise ValueError("body must be a JSON object")
        # 'repo' is no longer required, and no longer decides anything: the repository to
        # clone comes from the Grant. It is still type-checked, and still compared to the
        # Grant below — a caller that names a different repo is told so rather than
        # quietly getting the policy's one.
        for key in ("manifest", "repo", "ref", "base", "task", "model"):
            if key in spec and not isinstance(spec[key], str):
                raise ValueError(f"'{key}' must be a string")
        if spec.get("ref") and not valid_ref(spec["ref"]):
            raise ValueError("'ref' must be a branch, tag or commit sha")
        if spec.get("base") and not SHA_RE.fullmatch(spec["base"]):
            raise ValueError("'base' must be a full 40-character commit sha")
        if "wait" in spec and not isinstance(spec["wait"], bool):
            raise ValueError("'wait' must be a boolean")
        return spec

    def _owner(self, manifest_name: str) -> str:
        """Who is asking, as a task-ownership key. Raises oidc.InvalidToken (401) or
        authorize.Denied (403).

        Literally _grant_for's answer for an empty request under the named manifest, so
        /status applies exactly the rules /run does. The unauthenticated identity is a
        real key rather than None: during the migration window unauthenticated callers
        can read each other's results, and nobody else's.
        """
        return self._grant_for({"manifest": manifest_name} if manifest_name else {}).owner

    def _grant_for(self, spec: dict):
        """Authenticate the caller and turn the request into a Grant.

        Raises oidc.InvalidToken (401) or authorize.Denied (403). Splitting the two is
        deliberate: 401 means "I do not know who you are", 403 means "I do, and no".
        """
        header = self.headers.get("Authorization")
        if header is None and not REQUIRE_AUTH:
            # The migration window. Unauthenticated callers get the v1 policy's Grant,
            # which is the same clone target and model an authorized caller would get —
            # so flipping REQUIRE_AUTH changes who may call, never what a call can do.
            return authorize.Grant(
                caller="unauthenticated",
                manifest="",
                workflow_ref="",
                clone_repo=authorize.V1_CLONE_REPO,
                model=authorize.V1_MODEL,
                task=spec.get("task", ""),
                ref=spec.get("ref", ""),
                base=spec.get("base", ""),
            )
        claims = oidc.verify(oidc.bearer_token(header))
        return authorize.authorize(claims, spec, manifests())

    def do_POST(self) -> None:
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        try:
            spec = self._read_spec()
        except ValueError as exc:
            # json.JSONDecodeError is a ValueError subclass, so a malformed body, a
            # non-object body and a bad header all answer 400 rather than dropping the
            # connection with no response at all.
            self._send(400, {"error": str(exc)})
            return

        try:
            grant = self._grant_for(spec)
        except oidc.InvalidToken as exc:
            self._send(401, {"error": str(exc)})
            return
        except authorize.Denied as exc:
            self._send(403, {"error": str(exc)})
            return

        # authorize.py already refused an authenticated `repo` the manifest does not
        # allow; this catches the unauthenticated path, which is pinned to one repo. The
        # model is policy on both paths. Refused rather than ignored: a caller that asks
        # for a cheap model, gets the policy's, and is told nothing has been misled.
        #
        # Keyed on PRESENCE, not truthiness. `spec.get("repo")` skipped `"repo": ""`,
        # which disagrees with the policy repository as much as any other wrong value
        # does, and skipped `"model": ""` too — so the one `model` a caller could send
        # without a 403 was the one that silently meant "whatever you have configured".
        if "repo" in spec and spec["repo"] != grant.clone_repo:
            self._send(
                403,
                {"error": f"the repository to clone is set by policy ({grant.clone_repo}), not by the request"},
            )
            return

        if "model" in spec and spec["model"] != grant.model:
            self._send(403, {"error": "the model is set by policy, not by the request"})
            return

        if not kube.AGENT_IMAGE:
            self._send(500, {"error": "AGENT_IMAGE not set — deploy.sh did not substitute the task image"})
            return

        task_id = tasks.start_task(grant.owner)
        if task_id is None:
            self._send(429, {"error": f"at capacity: {tasks.MAX_CONCURRENT_TASKS} tasks in flight"})
            return

        # One line per admitted task, so who dispatched what is answerable from the pod
        # log rather than only from the Job that has since been deleted.
        print(
            f"[sandbox-dispatcher] task {task_id} granted to {grant.caller} under "
            f"{grant.manifest or 'no manifest'} ({grant.workflow_ref or 'no workflow'})"
        )

        if spec.get("wait", True):
            result = tasks.run_and_record(task_id, grant)
            # KNOWN GAP (deferred, see README § Limitations "A task can overwrite the
            # response fields the endpoints own"): on the success path `result` came
            # from the untrusted task pod's log, and spreading it last lets it replace
            # the task_id we just minted. Same shape as tasks.status(), and the same
            # response-schema decision — reordering here alone would drop a task's own
            # "task_id" field instead.
            self._send(200, {"task_id": task_id, **result})
            return

        tasks.run_in_background(task_id, grant)
        self._send(202, {"task_id": task_id, "state": "running"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[sandbox-dispatcher] {fmt % args}")


class HTTPServerV6(ThreadingHTTPServer):
    # OSDC EKS is IPv6-only: the pod IP (and the readiness probe / Service target) are
    # IPv6, so the listener must bind :: — a default AF_INET server binds 0.0.0.0 and
    # is unreachable on this cluster.
    #
    # Threading, because a synchronous /run holds its connection for the length of a
    # task: without it one caller would block /healthz, the readiness probe would time
    # out, and the pod would be dropped from the Service mid-task.
    address_family = socket.AF_INET6
