"""Tests for the sandbox dispatcher.

Stdlib-only, so nothing needs stubbing: the dispatcher runs against a fake Kubernetes
API on a real socket, and its HTTP surface is driven over a real socket too. The fake
API records what was created, which is where the Job-shape assertions come from — that
manifest is the security boundary for every task pod (gVisor, no token, capped disk).
"""

import json
import socket
import ssl
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import dispatcher
import pytest


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
    monkeypatch.setattr(dispatcher, "TOKEN_PATH", token)
    monkeypatch.setattr(dispatcher, "CA_PATH", tmp_path / "absent-ca.crt")
    monkeypatch.setattr(dispatcher, "_k8s_api", lambda: f"http://{host}:{port}")
    monkeypatch.setattr(dispatcher, "AGENT_IMAGE", "harbor:30002/osdc/ci-agent-sandbox:deadbeef")
    monkeypatch.setattr(dispatcher, "POLL_INTERVAL_S", 0)
    dispatcher._ssl_context.cache_clear()  # cached across calls; don't leak one between tests
    dispatcher._TASKS.clear()
    yield state
    httpd.shutdown()
    httpd.server_close()


class TestJobManifest:
    """The Job template is the security boundary for every task pod, so assert it
    rather than trusting that a manifest reviewed once stays that way."""

    def _pod_spec(self):
        return dispatcher.job_manifest("abc123abc123", {"repo": "org/repo"})["spec"]["template"]["spec"]

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
        job = dispatcher.job_manifest("abc123abc123", {"repo": "org/repo"})
        assert job["spec"]["backoffLimit"] == 0
        assert job["spec"]["activeDeadlineSeconds"] == dispatcher.TASK_DEADLINE_S
        assert job["spec"]["ttlSecondsAfterFinished"] == dispatcher.JOB_TTL_S

    def test_spec_is_passed_as_env_not_baked_into_the_image(self):
        spec = dispatcher.job_manifest("abc123abc123", {"repo": "org/repo", "ref": "v1", "model": "us.x"})
        env = {e["name"]: e["value"] for e in spec["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["SANDBOX_REPO"] == "org/repo"
        assert env["SANDBOX_REF"] == "v1"
        assert env["SANDBOX_MODEL"] == "us.x"
        assert env["SANDBOX_TASK"] == "", "an omitted task must arrive empty so run_task applies its default"

    def test_carries_the_task_image_the_dispatcher_was_given(self, monkeypatch):
        monkeypatch.setattr(dispatcher, "AGENT_IMAGE", "harbor:30002/osdc/ci-agent-sandbox:cafe1234")
        assert self._pod_spec()["containers"][0]["image"] == "harbor:30002/osdc/ci-agent-sandbox:cafe1234"

    def test_gvisor_label_matches_the_network_policy_selector(self):
        """sandbox-task-egress selects app=sandbox-task; a label drift here silently
        leaves task pods with no egress allow-list at all."""
        labels = dispatcher.job_manifest("abc123abc123", {"repo": "r"})["spec"]["template"]["metadata"]["labels"]
        assert labels["app"] == "sandbox-task"


class TestRunToCompletion:
    def test_creates_one_job_and_returns_the_parsed_result(self, fake_k8s):
        result = dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
        assert result == {"cloned": True, "report": "ok"}
        assert len(fake_k8s["jobs"]) == 1
        assert fake_k8s["jobs"][0]["metadata"]["name"] == "sandbox-task-abc123abc123"

    def test_deletes_the_job_after_reading_the_log(self, fake_k8s):
        """The log is the result transport, so the Job can only be collected after it
        has been read — and it must be, or finished Jobs pile up against the quota."""
        dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
        assert any("sandbox-task-abc123abc123" in path for path in fake_k8s["deleted"])

    def test_last_json_line_wins(self, fake_k8s):
        fake_k8s["log"] = 'warning: detached HEAD\n{"cloned": true, "file_count": 3}\n'
        assert dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})["file_count"] == 3

    def test_failed_pod_without_output_reports_the_reason(self, fake_k8s):
        fake_k8s["job_status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
        fake_k8s["log"] = "Killed\n"
        result = dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
        assert "DeadlineExceeded" in result["errors"]["task"]

    def test_failed_pod_that_did_print_a_result_keeps_it(self, fake_k8s):
        """A task whose clone failed still printed the errors object — that is the
        answer, and it must not be replaced by a generic pod failure."""
        fake_k8s["job_status"] = {"conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimit"}]}
        fake_k8s["log"] = '{"cloned": false, "errors": {"clone": "could not read Username"}}\n'
        result = dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
        assert result["errors"]["clone"] == "could not read Username"

    def test_api_failure_is_reported_not_raised(self, monkeypatch, fake_k8s):
        def boom(*a, **kw):
            raise dispatcher.ApiError("jobs.batch is forbidden")

        monkeypatch.setattr(dispatcher, "create_job", boom)
        result = dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
        assert "forbidden" in result["errors"]["dispatch"]


@pytest.fixture
def server(fake_k8s):
    """The real dispatcher HTTP surface on an ephemeral port (IPv6, as in-cluster)."""
    httpd = dispatcher.HTTPServerV6(("::1", 0), dispatcher.Handler)
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
        assert body["capacity"] == dispatcher.MAX_CONCURRENT_TASKS

    def test_healthz_counts_a_running_task(self, server):
        dispatcher._TASKS.clear()
        dispatcher.start_task()
        assert _get(f"{server}/healthz")["in_flight"] == 1

    def test_unknown_path_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(f"{server}/nope")
        assert exc.value.code == 404

    def test_post_to_unknown_path_is_404(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/nope", {"repo": "org/repo"})
        assert exc.value.code == 404

    def test_run_waits_and_returns_the_result(self, server):
        body = _post(f"{server}/run", {"repo": "org/repo"})
        assert body["cloned"] is True
        assert body["report"] == "ok"
        assert body["task_id"]

    def test_run_requires_repo(self, server):
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"task": "no repo given"})
        assert exc.value.code == 400
        assert "missing 'repo'" in json.loads(exc.value.read())["error"]

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
            _post(f"{server}/run", {"repo": "org/repo", field: value})
        assert exc.value.code == 400

    def test_async_run_returns_a_task_id_then_a_result(self, server):
        req = urllib.request.Request(  # noqa: S310
            f"{server}/run",
            data=json.dumps({"repo": "org/repo", "wait": False}).encode(),
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
        real_state = dispatcher.job_state

        def slow_state(task_id):
            gate.wait(10)
            return real_state(task_id)

        monkeypatch.setattr(dispatcher, "job_state", slow_state)
        results = []
        threads = [
            Thread(target=lambda: results.append(_post(f"{server}/run", {"repo": "org/repo"})), daemon=True)
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
        monkeypatch.setattr(dispatcher, "MAX_CONCURRENT_TASKS", 1)
        dispatcher._TASKS.clear()
        assert dispatcher.start_task() is not None  # occupy the only slot
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"repo": "org/repo"})
        assert exc.value.code == 429
        assert "at capacity" in json.loads(exc.value.read())["error"]

    def test_missing_task_image_is_500_not_a_broken_job(self, server, monkeypatch):
        monkeypatch.setattr(dispatcher, "AGENT_IMAGE", "")
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{server}/run", {"repo": "org/repo"})
        assert exc.value.code == 500
        assert "AGENT_IMAGE" in json.loads(exc.value.read())["error"]

    def test_finished_results_are_pruned(self, monkeypatch):
        """Results live in memory so /status can answer after the Job is gone — the one
        thing in a long-running dispatcher that would otherwise grow forever."""
        monkeypatch.setattr(dispatcher, "RESULT_RETENTION_S", 0)
        dispatcher._TASKS.clear()
        task_id = dispatcher.start_task()
        dispatcher._finish(task_id, {"report": "ok"})
        dispatcher.start_task()
        assert task_id not in dispatcher._TASKS


class TestInClusterConfig:
    """How the dispatcher finds the API server and authenticates to it."""

    def test_api_url_brackets_an_ipv6_host(self, monkeypatch):
        """OSDC EKS is IPv6-only, so KUBERNETES_SERVICE_HOST is a bare IPv6 address and
        an unbracketed URL is unparseable."""
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fdba:9e82:4cac::1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        assert dispatcher._k8s_api() == "https://[fdba:9e82:4cac::1]:443"

    def test_api_url_leaves_ipv4_alone(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.100.0.1")
        monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
        assert dispatcher._k8s_api() == "https://10.100.0.1:443"

    def test_api_url_outside_a_pod_raises(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        with pytest.raises(RuntimeError, match="KUBERNETES_SERVICE_HOST"):
            dispatcher._k8s_api()

    def test_missing_token_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(dispatcher, "TOKEN_PATH", tmp_path / "absent")
        with pytest.raises(RuntimeError, match="token not found"):
            dispatcher._read_token()

    def test_ssl_context_loads_the_projected_ca(self, monkeypatch, tmp_path):
        """Without the projected CA the API server's cert doesn't verify, and every
        call would fail closed."""
        monkeypatch.setattr(dispatcher, "CA_PATH", tmp_path / "absent")
        assert isinstance(dispatcher._ssl_context(), ssl.SSLContext)

    def test_api_error_carries_the_status_and_body(self, fake_k8s):
        """A 403 here means the Role is wrong — the message has to say so rather than
        surfacing as a bare failure."""
        with pytest.raises(dispatcher.ApiError, match="404"):
            dispatcher.api_request("GET", "/apis/batch/v1/namespaces/ai-sandbox/nope")


class TestPolling:
    def test_waits_while_the_job_is_running(self, fake_k8s, monkeypatch):
        states = iter([("running", ""), ("running", ""), ("succeeded", "")])
        monkeypatch.setattr(dispatcher, "job_state", lambda task_id: next(states))
        assert dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})["report"] == "ok"

    def test_gives_up_at_the_deadline(self, fake_k8s, monkeypatch):
        monkeypatch.setattr(dispatcher, "TASK_DEADLINE_S", -1)
        monkeypatch.setattr(dispatcher, "job_state", lambda task_id: ("running", ""))
        result = dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
        assert "did not finish" in result["errors"]["dispatch"]

    def test_running_job_reports_running(self, fake_k8s):
        fake_k8s["job_status"] = {"active": 1}
        assert dispatcher.job_state("abc123abc123") == ("running", "")

    def test_missing_pod_is_an_api_error(self, fake_k8s, monkeypatch):
        monkeypatch.setattr(
            dispatcher, "api_request", lambda method, path, **kw: {"items": []} if "/pods?" in path else {}
        )
        with pytest.raises(dispatcher.ApiError, match="no pod found"):
            dispatcher.task_result("abc123abc123")

    def test_log_without_json_is_an_api_error(self, fake_k8s):
        fake_k8s["log"] = "Traceback (most recent call last):\n  ImportError\n"
        with pytest.raises(dispatcher.ApiError, match="no result JSON"):
            dispatcher.task_result("abc123abc123")

    def test_a_brace_line_that_is_not_json_is_skipped(self, fake_k8s):
        """Anything the task or git writes to stdout lands in the same log, so a line
        that merely starts with { must not shadow the real result."""
        # Scanned last line first, so the trailing junk is what has to be skipped.
        fake_k8s["log"] = '{"cloned": true, "report": "the real one"}\n{not json at all\n'
        assert dispatcher.task_result("abc123abc123")["report"] == "the real one"

    def test_api_failure_while_polling_is_reported(self, fake_k8s, monkeypatch):
        def boom(task_id):
            raise dispatcher.ApiError("etcdserver: request timed out")

        monkeypatch.setattr(dispatcher, "job_state", boom)
        result = dispatcher.run_to_completion("abc123abc123", {"repo": "org/repo"})
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

        monkeypatch.setattr(dispatcher, "CA_PATH", ca)
        monkeypatch.setattr(dispatcher.ssl, "create_default_context", lambda: FakeCtx())
        dispatcher._ssl_context.cache_clear()
        dispatcher._ssl_context()
        assert loaded["cafile"] == str(ca)

        # Built once, not per API call: each in-flight task polls every POLL_INTERVAL_S,
        # and re-parsing the CA bundle every time is pure overhead.
        loaded.clear()
        dispatcher._ssl_context()
        assert loaded == {}, "the context must be cached, not rebuilt on every call"
        dispatcher._ssl_context.cache_clear()

    def test_delete_failure_is_logged_not_raised(self, fake_k8s, monkeypatch, capsys):
        """The TTL collects the Job anyway; failing the request over cleanup would
        throw away a result that is already in hand."""

        def boom(*a, **kw):
            raise dispatcher.ApiError("jobs.batch is forbidden")

        monkeypatch.setattr(dispatcher, "api_request", boom)
        dispatcher.delete_job("abc123abc123")
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
        response = self._raw_post(server, {"Content-Length": str(dispatcher.MAX_BODY_BYTES + 1)})
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

        monkeypatch.setattr(dispatcher, "HTTPServerV6", FakeServer)
        dispatcher.main()
        assert bound["address"] == ("::", dispatcher.PORT)
        assert bound["handler"] is dispatcher.Handler
        assert bound["served"] is True

    def test_binds_ipv6(self):
        """A default AF_INET listener binds 0.0.0.0 and is unreachable on the IPv6-only
        cluster — the readiness probe and the Service both target the pod's IPv6."""
        assert dispatcher.HTTPServerV6.address_family == socket.AF_INET6

    def test_handler_has_a_socket_timeout(self):
        assert 0 < dispatcher.Handler.timeout <= 60
