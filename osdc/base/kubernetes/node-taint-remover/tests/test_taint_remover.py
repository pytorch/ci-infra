"""Unit tests for taint_remover.py — stdlib-only urllib mocking."""

from __future__ import annotations

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


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://[::1]:443/api/v1/nodes/node-1",
        code=code,
        msg="x",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


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


def _instance_type_taint() -> dict:
    return {"key": "instance-type", "value": "x", "effect": "NoSchedule"}


def test_taint_already_absent():
    node = _node(taints=[_instance_type_taint()])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.return_value = _FakeResp(200, json.dumps(node).encode())
        taint_remover.remove_taint_forever("my-startup-taint")
        # Only GETs were issued — no PATCH.
        called_methods = [c.args[0].get_method() for c in urlopen.call_args_list]
        assert "PATCH" not in called_methods


def test_taint_removed_successfully():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # instance-type guard GET
            _FakeResp(200, json.dumps(node).encode()),  # main GET
            _FakeResp(200, b"{}"),  # PATCH
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 3
        patch_req = urlopen.call_args_list[-1].args[0]
        assert patch_req.get_method() == "PATCH"


def test_retry_on_transient_url_error():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            urllib.error.URLError("timeout"),  # instance-type guard GET fails
            _FakeResp(200, json.dumps(node).encode()),  # instance-type guard GET retry succeeds
            _FakeResp(200, json.dumps(node).encode()),  # main GET
            _FakeResp(200, b"{}"),  # PATCH
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 4


def test_retry_on_http_500():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # guard GET
            _FakeResp(200, json.dumps(node).encode()),  # main GET
            _http_error(500, b"server boom"),  # PATCH 500
            _FakeResp(200, json.dumps(node).encode()),  # guard GET retry
            _FakeResp(200, json.dumps(node).encode()),  # main GET retry
            _FakeResp(200, b"{}"),  # PATCH success
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 6


def test_permanent_error_on_http_403():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node = _node(taints=[_instance_type_taint(), target])
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node).encode()),  # guard GET
            _FakeResp(200, json.dumps(node).encode()),  # main GET
            _http_error(403, b"forbidden"),
        ]
        with pytest.raises(taint_remover.PermanentApiError, match="Permanent error"):
            taint_remover.remove_taint_forever("my-startup-taint")


def test_instance_type_guard_retries_until_present():
    target = {"key": "my-startup-taint", "value": "", "effect": "NoSchedule"}
    node_without = _node(taints=[target])  # label present, taint absent
    node_with = _node(taints=[_instance_type_taint(), target])  # both present
    with patch.object(taint_remover.urllib.request, "urlopen") as urlopen:
        urlopen.side_effect = [
            _FakeResp(200, json.dumps(node_without).encode()),  # guard sees no taint
            _FakeResp(200, json.dumps(node_with).encode()),  # guard sees taint
            _FakeResp(200, json.dumps(node_with).encode()),  # main GET
            _FakeResp(200, b"{}"),  # PATCH
        ]
        taint_remover.remove_taint_forever("my-startup-taint")
        assert urlopen.call_count == 4
        # No PATCH was issued during the first guard attempt.
        first_req = urlopen.call_args_list[0].args[0]
        assert first_req.get_method() == "GET"


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
