"""Unit tests for taint_remover.py — stdlib-only urllib mocking."""

from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
from unittest.mock import patch

import pytest
import taint_remover


class _FakeResp:
    """Minimal context-manager mock for urllib.request.urlopen()."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


def _node(taints: list[dict] | None = None, instance_type_label: bool = True) -> dict:
    labels = {}
    if instance_type_label:
        labels["instance-type"] = "m6i.large"
    return {
        "metadata": {"name": "node-1", "labels": labels},
        "spec": {"taints": taints or []},
    }


def _http_error(code: int, body: bytes = b"", retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://[::1]:443/api/v1/nodes/node-1",
        code=code,
        msg="x",
        hdrs=hdrs,
        fp=io.BytesIO(body),
    )


def _methods(urlopen) -> list[str]:
    return [c.args[0].get_method() for c in urlopen.call_args_list]


@pytest.fixture(autouse=True)
def _env_and_files(monkeypatch, tmp_path):
    """Default environment: in-cluster vars set, token file present, no CA."""
    monkeypatch.setenv("NODE_NAME", "node-1")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    token = tmp_path / "token"
    token.write_text("test-token")
    monkeypatch.setattr(taint_remover, "TOKEN_PATH", token)
    # No CA file — use default ctx (avoids touching real /var/run paths).
    monkeypatch.setattr(taint_remover, "CA_PATH", tmp_path / "ca.crt")
    # Eliminate real sleeps.
    monkeypatch.setattr(taint_remover.time, "sleep", lambda *_: None)
    # Deterministic jitter: always take the top of the [d/2, d] range.
    monkeypatch.setattr(taint_remover.random, "uniform", lambda _lo, hi: hi)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Record every backoff sleep instead of discarding it."""
    recorded: list[float] = []
    monkeypatch.setattr(taint_remover.time, "sleep", recorded.append)
    return recorded


def _instance_type_taint() -> dict:
    return {"key": "instance-type", "value": "x", "effect": "NoSchedule"}


def test_taint_already_absent():
    node = _node(taints=[_instance_type_taint()])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.return_value = _FakeResp(200, json.dumps(node).encode())
        taint_remover.remove_taint_forever("my-startup-taint")
        # Only a GET was issued — no PATCH.
        called_methods = [c.args[0].get_method() for c in urlopen.call_args_list]
        assert "PATCH" not in called_methods


def test_taint_removed_successfully():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _FakeResp(200, b"{}"),  # PATCH
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 2
        patch_req = urlopen.call_args_list[-1].args[0]
        assert patch_req.get_method() == "PATCH"


def test_retry_on_transient_url_error():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            urllib.error.URLError("timeout"),  # GET fails
            _FakeResp(200, json.dumps(node).encode()),  # GET retry succeeds
            _FakeResp(200, b"{}"),  # PATCH
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 3


def test_retry_on_http_500():
    """PATCH 5xx retries the PATCH without re-GETting the node."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(500, b"server boom"),  # PATCH 500
            _FakeResp(200, b"{}"),  # PATCH retry succeeds
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert _methods(urlopen) == ["GET", "PATCH", "PATCH"]


def test_retry_on_http_429_patch_throttled():
    """HTTP 429 on PATCH (APF / etcd throttling) is transient — retry the PATCH only."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(429, b"etcdserver: throttle: too many requests"),  # PATCH 429
            _FakeResp(200, b"{}"),  # PATCH retry succeeds
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert _methods(urlopen) == ["GET", "PATCH", "PATCH"]


def test_http_429_backoff_grows_exponentially_and_caps(sleeps):
    """Repeated 429s back off 5, 10, 20, ... up to the 300s cap, never re-GETting."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    throttled = [_http_error(429, b"too many requests") for _ in range(8)]
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [_FakeResp(200, json.dumps(node).encode()), *throttled, _FakeResp(200, b"{}")]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert _methods(urlopen) == ["GET"] + ["PATCH"] * 9
    assert sleeps == [5, 10, 20, 40, 80, 160, 300, 300]


def test_backoff_jitter_stays_in_upper_half(monkeypatch):
    """Jitter spreads retries within [d/2, d] so the delay still grows."""
    monkeypatch.setattr(taint_remover.random, "uniform", lambda lo, _hi: lo)
    assert [taint_remover._backoff(n) for n in range(4)] == [2.5, 5, 10, 20]


def test_network_timeouts_back_off_exponentially(sleeps):
    """Client timeouts (often APF queueing past our deadline) back off like a 429."""
    node = _node(taints=[_instance_type_taint()])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            TimeoutError("timed out"),
            urllib.error.URLError("timed out"),
            ConnectionResetError("reset"),
            TimeoutError("timed out"),
            _FakeResp(200, json.dumps(node).encode()),
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
    assert sleeps == [5, 10, 20, 40]


def test_race_retries_do_not_reset_backoff(sleeps):
    """A 422 between throttled PATCHes re-reads immediately but keeps the backoff growing."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(429, b"too many requests"),
            _http_error(422, b"the test operation failed"),
            _FakeResp(200, json.dumps(node).encode()),  # re-GET after 422
            _http_error(429, b"too many requests"),
            _FakeResp(200, b"{}"),
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert _methods(urlopen) == ["GET", "PATCH", "PATCH", "GET", "PATCH", "PATCH"]
    assert sleeps == [5, 10]


def test_http_429_honours_retry_after(sleeps):
    """A Retry-After longer than the computed backoff is respected (and capped)."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(429, b"slow down", retry_after="120"),
            _http_error(429, b"slow down", retry_after="3600"),
            _FakeResp(200, b"{}"),
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
    assert sleeps == [120, 300]


def test_get_http_503_backs_off(sleeps):
    """GET 5xx/429 also backs off exponentially."""
    node = _node(taints=[_instance_type_taint()])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _http_error(503, b"unavailable"),
            _http_error(429, b"too many requests"),
            _FakeResp(200, json.dumps(node).encode()),
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
    assert sleeps == [5, 10]


def test_permanent_error_on_http_404():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(404, b"not found"),
        ]
        with pytest.raises(taint_remover.PermanentApiError, match="Permanent error"):
            taint_remover.remove_taint_forever("my-startup-taint")


def test_permanent_error_on_http_403():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(403, b"forbidden"),
        ]
        with pytest.raises(taint_remover.PermanentApiError, match="Permanent error"):
            taint_remover.remove_taint_forever("my-startup-taint")


def test_retry_on_http_422_json_patch_test_failure():
    """HTTP 422 from a failed JSON Patch `test` op is a benign race — retry."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # GET
            _http_error(422, b"the test operation failed"),  # PATCH 422
            _FakeResp(200, json.dumps(node).encode()),  # GET retry
            _FakeResp(200, b"{}"),  # PATCH success
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 4


def test_retry_on_http_401_picks_up_rotated_token(tmp_path, monkeypatch):
    """HTTP 401 triggers a retry; the SA token is re-read so kubelet rotation is picked up."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])

    token_path = tmp_path / "rotating-token"
    token_path.write_text("old-token")
    monkeypatch.setattr(taint_remover, "TOKEN_PATH", token_path)

    auth_headers: list[str] = []

    def _capture(req, **_kwargs):
        auth_headers.append(req.get_header("Authorization"))
        if len(auth_headers) == 1:
            # GET succeeds with old token
            return _FakeResp(200, json.dumps(node).encode())
        if len(auth_headers) == 2:
            # PATCH returns 401 — simulate kubelet rotating the token here
            token_path.write_text("new-token")
            raise _http_error(401, b"unauthorized")
        # Subsequent calls (GET, PATCH) should use the new token
        if req.get_method() == "PATCH":
            return _FakeResp(200, b"{}")
        return _FakeResp(200, json.dumps(node).encode())

    with patch.object(taint_remover.urllib.request, "urlopen", side_effect=_capture):
        taint_remover.remove_taint_forever("my-startup-taint")

    # First two requests used the old token, the rest used the rotated one.
    assert auth_headers[0] == "Bearer old-token"
    assert auth_headers[1] == "Bearer old-token"
    assert auth_headers[2] == "Bearer new-token"
    assert auth_headers[-1] == "Bearer new-token"


def test_missing_node_name_env(monkeypatch, capsys):
    monkeypatch.delenv("NODE_NAME", raising=False)
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        # Even though urlopen is mocked, _node_name() is called before any HTTP.
        urlopen.return_value = _FakeResp(200, b"{}")
        rc = taint_remover.main.__wrapped__() if hasattr(taint_remover.main, "__wrapped__") else None
    # Use the actual main() with argv.
    with patch.object(sys, "argv", ["taint_remover.py", "some-taint"]):
        rc = taint_remover.main()
    assert rc == 1


def test_missing_sa_token(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(taint_remover, "TOKEN_PATH", missing)
    with patch.object(sys, "argv", ["taint_remover.py", "some-taint"]):
        rc = taint_remover.main()
    assert rc == 1


def test_json_patch_body_shape():
    """PATCH body must be RFC 6902 [test, remove] targeting the right index."""
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    other = {"key": "other-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), other, target])  # target at index 2
    captured: dict = {}

    def _capture(req, **kwargs):
        if req.get_method() == "PATCH":
            captured["body"] = req.data
            captured["content_type"] = req.get_header("Content-type")
        return (
            _FakeResp(200, b"{}")
            if req.get_method() == "PATCH"
            else _FakeResp(
                200,
                json.dumps(node).encode(),
            )
        )

    with patch.object(taint_remover.urllib.request, "urlopen", side_effect=_capture):
        taint_remover.remove_taint_forever("my-startup-taint")

    assert captured["content_type"] == "application/json-patch+json"
    body = json.loads(captured["body"])
    assert body == [
        {"op": "test", "path": "/spec/taints/2/key", "value": "my-startup-taint"},
        {"op": "remove", "path": "/spec/taints/2"},
    ]


def test_ipv6_api_host(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    captured: list[str] = []

    node = _node(taints=[_instance_type_taint()])

    def _capture(req, **kwargs):
        captured.append(req.full_url)
        return _FakeResp(200, json.dumps(node).encode())

    with patch.object(taint_remover.urllib.request, "urlopen", side_effect=_capture):
        taint_remover.remove_taint_forever("absent-taint")

    assert captured, "no requests captured"
    assert all(u.startswith("https://[fd00::1]:443/") for u in captured), captured
