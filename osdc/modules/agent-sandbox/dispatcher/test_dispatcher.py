"""Tests for the sandbox dispatcher.

Stdlib-only, so nothing needs stubbing: the dispatcher runs against a fake Kubernetes
API on a real socket, and its HTTP surface is driven over a real socket too. The fake
API records what was created, which is where the Job-shape assertions come from — that
manifest is the security boundary for every task pod (gVisor, no token, capped disk).

One file for four modules, deliberately: these are the same assertions the single-file
dispatcher carried, against the same behaviour, which is what makes the split reviewable
as a move rather than a rewrite.
"""

import importlib.util
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import authorize
import http_api
import jwt
import kube
import oidc
import pytest
import tasks
import test_authorize
import test_oidc


def _load_entrypoint():
    """__main__.py by path, under another name.

    A plain `import __main__` resolves to whatever is running the process — pytest — so
    the entry point is the one module here that cannot be imported the normal way. It is
    still worth a test: it decides the listen address, and binding IPv4 on this
    IPv6-only cluster is a silent, total outage.
    """
    spec = importlib.util.spec_from_file_location("dispatcher_entrypoint", Path(__file__).parent / "__main__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entrypoint = _load_entrypoint()


def a_grant(task="", ref="", model="", caller="unauthenticated"):
    """The Grant an unauthenticated caller gets today. job_manifest takes one of these
    rather than a request body, which is the layering rule made unavoidable."""
    return authorize.Grant(
        caller=caller,
        workflow_ref="",
        clone_repo="org/repo",
        model=model,
        task=task,
        ref=ref,
    )


@pytest.fixture
def fake_k8s(monkeypatch, tmp_path):
    """Stand in for the API server: record Jobs, answer polls, serve a pod log."""
    state = {"jobs": [], "deleted": [], "job_status": {"succeeded": 1}, "log": '{"cloned": true, "report": "ok"}\n'}

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload, code=200):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["jobs"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self._json({"metadata": {"name": state["jobs"][-1]["metadata"]["name"]}}, code=201)

        def do_DELETE(self):
            state["deleted"].append(self.path)
            self._json({"status": "Success"})

        def do_GET(self):
            if "/pods/" in self.path and self.path.endswith("/log"):
                body = state["log"].encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif "/pods?" in self.path:
                self._json({"items": [{"metadata": {"name": "sandbox-task-abc-xyz"}}]})
            elif "/jobs/" in self.path:
                self._json({"status": state["job_status"]})
            else:
                self._json({}, code=404)

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address

    # The dispatcher builds the API URL from the in-cluster env vars and reads the
    # projected token off disk; point both at the fake.
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", host)
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", str(port))
    token = tmp_path / "token"
    token.write_text("fake-token")
    monkeypatch.setattr(kube, "TOKEN_PATH", token)
    monkeypatch.setattr(kube, "CA_PATH", tmp_path / "absent-ca.crt")
    monkeypatch.setattr(kube, "_k8s_api", lambda: f"http://{host}:{port}")
    monkeypatch.setattr(kube, "AGENT_IMAGE", "harbor:30002/osdc/ci-agent-sandbox:deadbeef")
    monkeypatch.setattr(tasks, "POLL_INTERVAL_S", 0)
    kube._ssl_context.cache_clear()  # cached across calls; don't leak one between tests
    tasks._TASKS.clear()
    yield state
    httpd.shutdown()
    httpd.server_close()


class TestJobManifest:
    """The Job template is the security boundary for every task pod, so assert it
    rather than trusting that a manifest reviewed once stays that way."""

    def _pod_spec(self):
        return kube.job_manifest("abc123abc123", a_grant())["spec"]["template"]["spec"]

    def test_runs_under_gvisor_on_the_sandbox_fleet(self):
        assert self._pod_spec()["runtimeClassName"] == "gvisor"

    def test_task_pod_gets_no_kubernetes_identity(self):
        """The dispatcher holds the RBAC; a task pod that could reach the API server
        would hand the untrusted side the ability to create its own pods."""
        spec = self._pod_spec()
        assert spec["serviceAccountName"] == "sandbox-agent"
        assert spec["automountServiceAccountToken"] is False

    def test_requests_equal_limits_and_disk_is_capped(self):
        resources = self._pod_spec()["containers"][0]["resources"]
        assert resources["requests"] == resources["limits"]
        assert resources["requests"]["ephemeral-storage"]

    def test_no_retry(self):
        """A retry re-clones and re-prompts the model — and bills for it. The result
        object already carries per-stage errors."""
        job = kube.job_manifest("abc123abc123", a_grant())
        assert job["spec"]["backoffLimit"] == 0
        assert job["spec"]["activeDeadlineSeconds"] == kube.TASK_DEADLINE_S
        assert job["spec"]["ttlSecondsAfterFinished"] == kube.JOB_TTL_S

    def test_spec_is_passed_as_env_not_baked_into_the_image(self):
        spec = kube.job_manifest("abc123abc123", a_grant(ref="v1", model="us.x"))
        env = {e["name"]: e["value"] for e in spec["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["SANDBOX_REPO"] == "org/repo"
        assert env["SANDBOX_REF"] == "v1"
        assert env["SANDBOX_MODEL"] == "us.x"
        assert env["SANDBOX_TASK"] == "", "an omitted task must arrive empty so run_task applies its default"

    def test_carries_the_task_image_the_dispatcher_was_given(self, monkeypatch):
        monkeypatch.setattr(kube, "AGENT_IMAGE", "harbor:30002/osdc/ci-agent-sandbox:cafe1234")
        assert self._pod_spec()["containers"][0]["image"] == "harbor:30002/osdc/ci-agent-sandbox:cafe1234"

    def test_gvisor_label_matches_the_network_policy_selector(self):
        """sandbox-task-egress selects app=sandbox-task; a label drift here silently
        leaves task pods with no egress allow-list at all."""
        labels = kube.job_manifest("abc123abc123", a_grant())["spec"]["template"]["metadata"]["labels"]
        assert labels["app"] == "sandbox-task"


class TestRunToCompletion:
    def test_creates_one_job_and_returns_the_parsed_result(self, fake_k8s):
        result = tasks._run_to_completion("abc123abc123", a_grant())
        assert result == {"cloned": True, "report": "ok"}
        assert len(fake_k8s["jobs"]) == 1
        assert fake_k8s["jobs"][0]["metadata"]["name"] == "sandbox-task-abc123abc123"

    def test_deletes_the_job_after_reading_the_log(self, fake_k8s):
        """The log is the result transport, so the Job can only be collected after it
        has been read — and it must be, or finished Jobs pile up against the quota."""
        tasks._run_to_completion("abc123abc123", a_grant())
        assert any("sandbox-task-abc123abc123" in path for path in fake_k8s["deleted"])

    def test_last_json_line_wins(self, fake_k8s):
        fake_k8s["log"] = 'warning: detached HEAD\n{"cloned": true, "file_count": 3}\n'
        assert tasks._run_to_completion("abc123abc123", a_grant())["file_count"] == 3

    def test_failed_pod_without_output_reports_the_reason(self, fake_k8s):
        fake_k8s["job_status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
        fake_k8s["log"] = "Killed\n"
        result = tasks._run_to_completion("abc123abc123", a_grant())
        assert "DeadlineExceeded" in result["errors"]["task"]

    def test_failed_pod_that_did_print_a_result_keeps_it(self, fake_k8s):
        """A task whose clone failed still printed the errors object — that is the
        answer, and it must not be replaced by a generic pod failure."""
        fake_k8s["job_status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimit"}]}
        fake_k8s["log"] = '{"cloned": false, "errors": {"clone": "could not read Username"}}\n'
        result = tasks._run_to_completion("abc123abc123", a_grant())
        assert result["errors"]["clone"] == "could not read Username"

    def test_api_failure_is_reported_not_raised(self, monkeypatch, fake_k8s):
        def boom(*a, **kw):
            raise kube.ApiError("jobs.batch is forbidden")

        monkeypatch.setattr(kube, "create_job", boom)
        result = tasks._run_to_completion("abc123abc123", a_grant())
        assert "forbidden" in result["errors"]["dispatch"]


@pytest.fixture
def server(fake_k8s):
    """The real dispatcher HTTP surface on an ephemeral port (IPv6, as in-cluster)."""
    httpd = http_api.HTTPServerV6(("::1", 0), http_api.Handler)
    Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://[::1]:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


# Ignore any ambient HTTP(S)_PROXY — these requests go to a loopback socket.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url):
    return json.loads(_opener.open(url, timeout=10).read())


def _post(url, payload):
    req = urllib.request.Request(  # noqa: S310  (loopback http:// built in-test)
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    return json.loads(_opener.open(req, timeout=30).read())


class TestHTTPSurface:
    def test_healthz_reports_capacity(self, server):
        """A 429 is otherwise indistinguishable from a wedged dispatcher."""
        body = _get(f"{server}/healthz")
        assert body["status"] == "ok"
        assert body["in_flight"] == 0
        assert body["capacity"] == tasks.MAX_CONCURRENT_TASKS

    def test_healthz_counts_a_running_task(self, server):
        tasks._TASKS.clear()
        tasks.start_task("unauthenticated")
        assert _get(f"{server}/healthz")["in_flight"] == 1

    def test_unknown_path_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{server}/nope")
        assert exc.value.code == 404

    def test_post_to_unknown_path_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/nope", {"task": "hello"})
        assert exc.value.code == 404

    def test_run_waits_and_returns_the_result(self, server):
        body = _post(f"{server}/run", {"task": "hello"})
        assert body["cloned"] is True
        assert body["report"] == "ok"
        assert body["task_id"]

    def test_the_caller_cannot_choose_the_repository(self, server):
        """The repository to clone is policy, not request. A caller naming a different
        one is refused outright rather than quietly given the policy's."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"repo": "attacker/evil"})
        assert exc.value.code == 403
        assert "set by policy" in json.loads(exc.value.read())["error"]

    def test_naming_the_policy_repository_is_still_accepted(self, server):
        """The existing caller sends repo explicitly; it keeps working as long as it
        agrees with policy."""
        assert _post(f"{server}/run", {"repo": authorize.V1_CLONE_REPO})["report"] == "ok"

    @pytest.mark.parametrize("body", [[], None, 3, "text"], ids=["list", "null", "number", "string"])
    def test_non_object_body_is_400(self, server, body):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", body)
        assert exc.value.code == 400

    @pytest.mark.parametrize(
        ("field", "value"),
        [("ref", None), ("task", []), ("model", {}), ("wait", "yes")],
        ids=["ref-null", "task-list", "model-object", "wait-string"],
    )
    def test_wrong_field_type_is_400(self, server, field, value):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {field: value})
        assert exc.value.code == 400

    def test_async_run_returns_a_task_id_then_a_result(self, server):
        req = urllib.request.Request(  # noqa: S310
            f"{server}/run",
            data=json.dumps({"task": "hello", "wait": False}).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = _opener.open(req, timeout=10)
        assert resp.status == 202
        task_id = json.loads(resp.read())["task_id"]

        for _ in range(100):
            status = _get(f"{server}/status/{task_id}")
            if status["state"] == "done":
                assert status["report"] == "ok"
                return
        pytest.fail("async task never reported done")

    def test_status_rejects_a_malformed_id(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{server}/status/../../etc/passwd")
        assert exc.value.code in (400, 404)

    def test_status_of_an_unknown_id_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{server}/status/0123456789ab")
        assert exc.value.code == 404


class TestCapacity:
    def test_tasks_run_in_parallel(self, server, fake_k8s, monkeypatch):
        """The point of the design: concurrent requests must each get their own Job
        instead of queueing behind one slot."""
        gate = threading.Event()
        real_state = kube.job_state

        def slow_state(task_id):
            gate.wait(10)
            return real_state(task_id)

        monkeypatch.setattr(kube, "job_state", slow_state)
        results = []
        threads = [
            Thread(target=lambda: results.append(_post(f"{server}/run", {"task": "hello"})), daemon=True)
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        # All three Jobs exist before any of them is allowed to finish.
        for _ in range(200):
            if len(fake_k8s["jobs"]) == 3:
                break
            threading.Event().wait(0.05)
        assert len(fake_k8s["jobs"]) == 3, f"expected 3 Jobs in flight, got {len(fake_k8s['jobs'])}"
        gate.set()
        for t in threads:
            t.join(20)
        assert len(results) == 3
        assert {r["report"] for r in results} == {"ok"}

    def test_over_capacity_is_429(self, server, monkeypatch):
        """/run is unauthenticated, so the cap is what stops a caller loop turning into
        unbounded Jobs and, through Karpenter, unbounded nodes."""
        monkeypatch.setattr(tasks, "MAX_CONCURRENT_TASKS", 1)
        tasks._TASKS.clear()
        assert tasks.start_task("unauthenticated") is not None  # occupy the only slot
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"task": "hello"})
        assert exc.value.code == 429
        assert "at capacity" in json.loads(exc.value.read())["error"]

    def test_missing_task_image_is_500_not_a_broken_job(self, server, monkeypatch):
        monkeypatch.setattr(kube, "AGENT_IMAGE", "")
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"task": "hello"})
        assert exc.value.code == 500
        assert "AGENT_IMAGE" in json.loads(exc.value.read())["error"]

    def test_finished_results_are_pruned(self, monkeypatch):
        """Results live in memory so /status can answer after the Job is gone — the one
        thing in a long-running dispatcher that would otherwise grow forever."""
        monkeypatch.setattr(tasks, "RESULT_RETENTION_S", 0)
        tasks._TASKS.clear()
        task_id = tasks.start_task("unauthenticated")
        tasks._finish(task_id, {"report": "ok"})
        tasks.start_task("unauthenticated")
        assert task_id not in tasks._TASKS


class TestInClusterConfig:
    """How the dispatcher finds the API server and authenticates to it."""

    def test_api_url_brackets_an_ipv6_host(self, monkeypatch):
        """OSDC EKS is IPv6-only, so KUBERNETES_SERVICE_HOST is a bare IPv6 address and
        an unbracketed URL is unparseable."""
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fdba:9e82:4cac::1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        assert kube._k8s_api() == "https://[fdba:9e82:4cac::1]:443"

    def test_api_url_leaves_ipv4_alone(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        assert kube._k8s_api() == "https://10.100.0.1:443"

    def test_api_url_outside_a_pod_raises(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        with pytest.raises(RuntimeError, match="KUBERNETES_SERVICE_HOST"):
            kube._k8s_api()

    def test_missing_token_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(kube, "TOKEN_PATH", tmp_path / "absent")
        with pytest.raises(RuntimeError, match="token not found"):
            kube._read_token()

    def test_ssl_context_loads_the_projected_ca(self, monkeypatch, tmp_path):
        """Without the projected CA the API server's cert doesn't verify, and every
        call would fail closed."""
        monkeypatch.setattr(kube, "CA_PATH", tmp_path / "absent")
        assert isinstance(kube._ssl_context(), ssl.SSLContext)

    def test_api_error_carries_the_status_and_body(self, fake_k8s):
        """A 403 here means the Role is wrong — the message has to say so rather than
        surfacing as a bare failure."""
        with pytest.raises(kube.ApiError, match="404"):
            kube.api_request("GET", "/apis/batch/v1/namespaces/ai-sandbox/nope")


class TestPolling:
    def test_waits_while_the_job_is_running(self, fake_k8s, monkeypatch):
        states = iter([("running", ""), ("running", ""), ("succeeded", "")])
        monkeypatch.setattr(kube, "job_state", lambda task_id: next(states))
        assert tasks._run_to_completion("abc123abc123", a_grant())["report"] == "ok"

    def test_gives_up_at_the_deadline(self, fake_k8s, monkeypatch):
        monkeypatch.setattr(kube, "TASK_DEADLINE_S", -1)
        monkeypatch.setattr(kube, "job_state", lambda task_id: ("running", ""))
        result = tasks._run_to_completion("abc123abc123", a_grant())
        assert "did not finish" in result["errors"]["dispatch"]

    def test_running_job_reports_running(self, fake_k8s):
        fake_k8s["job_status"] = {"active": 1}
        assert kube.job_state("abc123abc123") == ("running", "")

    def test_missing_pod_is_an_api_error(self, fake_k8s, monkeypatch):
        monkeypatch.setattr(kube, "api_request", lambda method, path, **kw: {"items": []} if "/pods?" in path else {})
        with pytest.raises(kube.ApiError, match="no pod found"):
            kube.task_result("abc123abc123")

    def test_log_without_json_is_an_api_error(self, fake_k8s):
        fake_k8s["log"] = "Traceback (most recent call last):\n  ImportError\n"
        with pytest.raises(kube.ApiError, match="no result JSON"):
            kube.task_result("abc123abc123")

    def test_a_brace_line_that_is_not_json_is_skipped(self, fake_k8s):
        """Anything the task or git writes to stdout lands in the same log, so a line
        that merely starts with { must not shadow the real result."""
        # Scanned last line first, so the trailing junk is what has to be skipped.
        fake_k8s["log"] = '{"cloned": true, "report": "the real one"}\n{not json at all\n'
        assert kube.task_result("abc123abc123")["report"] == "the real one"

    def test_api_failure_while_polling_is_reported(self, fake_k8s, monkeypatch):
        def boom(task_id):
            raise kube.ApiError("etcdserver: request timed out")

        monkeypatch.setattr(kube, "job_state", boom)
        result = tasks._run_to_completion("abc123abc123", a_grant())
        assert "timed out" in result["errors"]["dispatch"]

    def test_ssl_context_loads_a_present_ca(self, monkeypatch, tmp_path):
        """Without the projected CA loaded, the API server's certificate does not
        verify and every call fails closed."""
        ca = tmp_path / "ca.crt"
        ca.write_text("-----BEGIN CERTIFICATE-----\n")
        loaded = {}

        class FakeCtx:
            def load_verify_locations(self, cafile):
                loaded["cafile"] = cafile

        monkeypatch.setattr(kube, "CA_PATH", ca)
        monkeypatch.setattr(kube.ssl, "create_default_context", lambda: FakeCtx())
        kube._ssl_context.cache_clear()
        kube._ssl_context()
        assert loaded["cafile"] == str(ca)

        # Built once, not per API call: each in-flight task polls every POLL_INTERVAL_S,
        # and re-parsing the CA bundle every time is pure overhead.
        loaded.clear()
        kube._ssl_context()
        assert loaded == {}, "the context must be cached, not rebuilt on every call"
        kube._ssl_context.cache_clear()

    def test_delete_failure_is_logged_not_raised(self, fake_k8s, monkeypatch, capsys):
        """The TTL collects the Job anyway; failing the request over cleanup would
        throw away a result that is already in hand."""

        def boom(*a, **kw):
            raise kube.ApiError("jobs.batch is forbidden")

        monkeypatch.setattr(kube, "api_request", boom)
        kube.delete_job("abc123abc123")
        assert "TTL will collect it" in capsys.readouterr().out


class TestRequestBodyLimits:
    """The body is read before any Job is created, on a thread ThreadingHTTPServer does
    not cap, from a declared length the caller controls. /run is unauthenticated."""

    @staticmethod
    def _raw_post(server, headers, body=b""):
        port = int(server.rsplit(":", 1)[1])
        request = f"POST /run HTTP/1.1\r\nHost: [::1]:{port}\r\n"
        request += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        with socket.create_connection(("::1", port), timeout=10) as sock:
            sock.sendall(request.encode() + b"\r\n" + body)
            received = b""
            while chunk := sock.recv(8192):
                received += chunk
        return received.decode(errors="replace")

    def test_non_numeric_content_length_is_400(self, server):
        response = self._raw_post(server, {"Content-Length": "abc"})
        assert "400" in response.splitlines()[0]
        assert "invalid Content-Length" in response

    def test_negative_content_length_is_400(self, server):
        """int() accepts -1 and read(-1) reads to end of file, which would park a
        handler thread for as long as the caller stays connected."""
        response = self._raw_post(server, {"Content-Length": "-1"})
        assert "400" in response.splitlines()[0]

    def test_oversized_content_length_is_refused_before_reading(self, server):
        response = self._raw_post(server, {"Content-Length": str(http_api.MAX_BODY_BYTES + 1)})
        assert "400" in response.splitlines()[0]


class TestServerShape:
    def test_main_serves_on_every_ipv6_interface(self, monkeypatch):
        bound = {}

        class FakeServer:
            def __init__(self, address, handler):
                bound["address"] = address
                bound["handler"] = handler

            def serve_forever(self):
                bound["served"] = True

        monkeypatch.setattr(http_api, "HTTPServerV6", FakeServer)
        entrypoint.main()
        assert bound["address"] == ("::", entrypoint.PORT)
        assert bound["handler"] is http_api.Handler
        assert bound["served"] is True

    def test_binds_ipv6(self):
        """A default AF_INET listener binds 0.0.0.0 and is unreachable on the IPv6-only
        cluster — the readiness probe and the Service both target the pod's IPv6."""
        assert http_api.HTTPServerV6.address_family == socket.AF_INET6

    def test_handler_has_a_socket_timeout(self):
        assert 0 < http_api.Handler.timeout <= 60


RUNTIME_DIR = "/usr/local/lib/sandbox-dispatcher/"


class TestImageContents:
    """The module set is written down twice and nothing used to compare the two copies.

    deploy.sh derives the image tag from the contents of this directory, so editing or
    adding a module rolls the tag and a new image is built and pushed. The Dockerfile
    names the files to copy by hand, so a module reaches that image only if someone also
    edits the COPY line. Nothing before this test compared the two, and the cheapest way
    to find out was a pod that cannot import what it needs.

    The hand-written list is deliberate — it is what keeps a test file out of the image
    and makes an addition visible in review. This is the check that makes it safe, and it
    is strict about the form on purpose: one build stage, exact filenames, copied into
    RUNTIME_DIR, and nothing else. A Dockerfile that switches to a glob source or a
    pruned build context (the modules/zombie-cleanup pattern) is a different design and
    should replace this test rather than be made to satisfy it.
    """

    @staticmethod
    def _dockerfile() -> str:
        text = (Path(__file__).parent / "Dockerfile").read_text().replace("\\\n", " ")
        assert len(re.findall(r"(?m)^FROM ", text)) == 1, (
            "the Dockerfile has more than one build stage; this test reads every COPY as landing "
            "in the final image, which is no longer true"
        )
        assert not re.findall(r"(?im)^\s*(?!COPY )copy\s", text), "a lowercase `copy` instruction would be missed here"
        return text

    @classmethod
    def _copied_into_runtime_dir(cls) -> set[str]:
        """Exactly what the Dockerfile places in RUNTIME_DIR, as written."""
        copied = set()
        for line in cls._dockerfile().splitlines():
            if not line.startswith("COPY "):
                continue
            args = line.split()[1:]
            assert not any(a.startswith("--from") for a in args), (
                "a --from= COPY brings files out of another build stage, which this test cannot resolve"
            )
            args = [a for a in args if not a.startswith("--")]
            if args[-1] != RUNTIME_DIR:
                continue
            copied.update(args[:-1])
        return copied

    @staticmethod
    def _hashed_modules() -> set[str]:
        """The same selection deploy.sh's _hash_dir() makes: every *.py under this
        directory, recursively, that is not a test file."""
        root = Path(__file__).parent
        return {
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if not p.name.startswith("test_") and p.name != "conftest.py"
        }

    def test_the_image_holds_exactly_the_modules_deploy_sh_hashes(self):
        copied, hashed = self._copied_into_runtime_dir(), self._hashed_modules()
        assert copied == hashed, {
            "hashed into the image tag but never copied into it (the pod cannot import these)": sorted(hashed - copied),
            "copied into the image but not a module (a test file, a glob, or a stale name)": sorted(copied - hashed),
            "fix": f"the COPY into {RUNTIME_DIR} in dispatcher/Dockerfile must name exactly these modules",
        }


class TestAuthenticatedSurface:
    """The auth path over the real HTTP surface, with real signed tokens.

    test_oidc.py proves the verifier; this proves the endpoints are wired to it — which
    is a different claim, and the one that would silently regress.
    """

    @pytest.fixture
    def signed(self, tmp_path, monkeypatch):
        keys = {test_oidc.KID: test_oidc._keypair()}
        path = tmp_path / "jwks.json"
        path.write_text(json.dumps(test_oidc._jwks_document(keys)))
        monkeypatch.setattr(oidc, "JWKS_PATH", path)
        oidc._CACHE.update(keyset=None, loaded_at=0.0, fetched_at=None)
        return keys

    def _authed_post(self, server, token, payload):
        req = urllib.request.Request(  # noqa: S310  (loopback http:// built in-test)
            f"{server}/run",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        return json.loads(_opener.open(req, timeout=30).read())

    def _token(self, keys, **overrides):
        return test_oidc.a_token(keys, **{**test_authorize.GOOD_CLAIMS, **overrides})

    def test_a_real_token_from_the_allowed_caller_runs_a_task(self, server, signed):
        assert self._authed_post(server, self._token(signed), {"task": "hello"})["report"] == "ok"

    def test_a_token_from_another_repository_is_403(self, server, signed):
        """Authenticated but not authorized — a different code from a bad signature,
        because they are different problems for whoever is reading the log."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._authed_post(server, self._token(signed, repository_id="999"), {"task": "hello"})
        assert exc.value.code == 403

    def test_a_forged_token_is_401_even_while_auth_is_optional(self, server, signed):
        """The migration flag governs the no-token case only. A token that IS presented
        is always verified, so turning enforcement on later cannot be the moment a
        forged token starts being rejected."""
        assert http_api.REQUIRE_AUTH is False
        stranger = test_oidc._keypair()
        forged = jwt.encode(
            {**test_authorize.GOOD_CLAIMS, "iss": oidc.ISSUER, "aud": oidc.AUDIENCE, "exp": int(time.time()) + 300},
            stranger,
            algorithm="RS256",
            headers={"kid": test_oidc.KID},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._authed_post(server, forged, {"task": "hello"})
        assert exc.value.code == 401

    def test_requiring_auth_refuses_a_request_with_no_token(self, server, monkeypatch):
        monkeypatch.setattr(http_api, "REQUIRE_AUTH", True)
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"task": "hello"})
        assert exc.value.code == 401

    def test_status_does_not_expose_another_callers_task(self, server, signed, monkeypatch):
        """/run and /status were both unauthenticated; fixing only /run would leave the
        results readable."""
        task_id = tasks.start_task("someone-else")
        tasks._finish(task_id, {"report": "secret"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{server}/status/{task_id}")
        assert exc.value.code == 404, "another caller's task must look absent, not forbidden"

    def test_a_caller_can_read_its_own_task(self, server):
        task_id = tasks.start_task("unauthenticated")
        tasks._finish(task_id, {"report": "mine"})
        assert _get(f"{server}/status/{task_id}")["report"] == "mine"
