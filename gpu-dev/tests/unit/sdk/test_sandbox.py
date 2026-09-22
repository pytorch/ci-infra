"""Unit tests for gpu_dev._sync.sandbox.Sandbox.

Covers exec/upload/download (transport delegation), cancel, extend, add_user,
refresh, the _ensure_active / _get_transport guard logic, the context-manager
auto-cancel, properties, repr, and pod_logs. Everything that would talk to a
real host (SshTransport) or AWS (Backend) is mocked, so nothing connects.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gpu_dev._sync.sandbox import Sandbox
from gpu_dev.common.enums import ReservationStatus
from gpu_dev.common.errors import GpuDevError, GpuDevTimeoutError
from gpu_dev.common.models import ExecResult, ReservationInfo


# ── helpers ────────────────────────────────────────────────────────────────


def make_info(**overrides):
    """Build a ReservationInfo with sane active defaults; override per-test."""
    base = dict(
        id="abc12345-dead-beef-0000-111122223333",
        status=ReservationStatus.ACTIVE,
        gpu_type="h100",
        gpu_count=2,
        name="my-box",
        pod_name="gpu-dev-abc12345",
        fqdn="gpu-dev-abc12345.example.com",
        ssh_command="ssh dev@host",
        expires_at="2026-06-01T00:00:00",
        jupyter_url="http://jupyter",
        disk_name="default",
        detailed_status="running",
        instance_type="p5.48xlarge",
        created_at="2026-05-31T00:00:00",
        user_id="user-1",
    )
    base.update(overrides)
    return ReservationInfo(**base)


def make_sandbox(info=None, backend=None, user_id="user-1"):
    return Sandbox(
        info=info or make_info(),
        backend=backend or MagicMock(),
        user_id=user_id,
    )


@pytest.fixture
def mock_transport():
    """Patch SshTransport where Sandbox looks it up; yield the instance mock."""
    with patch("gpu_dev._sync.sandbox.SshTransport") as cls:
        instance = MagicMock(name="transport_instance")
        cls.return_value = instance
        yield cls, instance


# ── exec / transport plumbing ──────────────────────────────────────────────


def test_exec_returns_exec_result_from_transport(mock_transport):
    cls, instance = mock_transport
    expected = ExecResult(exit_code=0, stdout="hi\n", stderr="")
    instance.exec.return_value = expected

    sb = make_sandbox()
    result = sb.exec("echo hi")

    assert result is expected
    assert result.exit_code == 0
    assert result.stdout == "hi\n"
    assert result.stderr == ""
    # transport built from pod_name + fqdn, then exec called with default timeout
    cls.assert_called_once_with("gpu-dev-abc12345", "gpu-dev-abc12345.example.com")
    instance.exec.assert_called_once_with("echo hi", timeout=None)


def test_exec_passes_timeout_through(mock_transport):
    _cls, instance = mock_transport
    instance.exec.return_value = ExecResult(exit_code=7, stdout="", stderr="boom")

    sb = make_sandbox()
    result = sb.exec("sleep 1", timeout=30)

    instance.exec.assert_called_once_with("sleep 1", timeout=30)
    assert result.exit_code == 7
    assert result.stderr == "boom"


def test_exec_nonzero_exit_code_returned_not_raised(mock_transport):
    _cls, instance = mock_transport
    instance.exec.return_value = ExecResult(exit_code=1, stdout="", stderr="nope")

    sb = make_sandbox()
    result = sb.exec("false")

    # A failing command is a normal ExecResult, not an exception.
    assert result.exit_code == 1


def test_transport_is_cached_across_calls(mock_transport):
    cls, instance = mock_transport
    instance.exec.return_value = ExecResult(exit_code=0)

    sb = make_sandbox()
    sb.exec("a")
    sb.exec("b")

    # SshTransport constructed exactly once, reused for both execs.
    assert cls.call_count == 1
    assert instance.exec.call_count == 2


def test_get_transport_raises_when_no_pod_name(mock_transport):
    cls, _instance = mock_transport
    sb = make_sandbox(make_info(pod_name=None))

    with pytest.raises(GpuDevError, match="no pod assigned yet"):
        sb.exec("echo hi")
    cls.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [ReservationStatus.CANCELLED, ReservationStatus.EXPIRED, ReservationStatus.FAILED],
)
def test_exec_blocked_on_inactive_status(mock_transport, status):
    cls, _instance = mock_transport
    sb = make_sandbox(make_info(status=status))

    with pytest.raises(GpuDevError, match=f"Sandbox is {status.value}"):
        sb.exec("echo hi")
    cls.assert_not_called()


def test_exec_allowed_for_non_active_but_non_terminal_status(mock_transport):
    # PENDING/QUEUED/PREPARING are not in the terminal set, so _ensure_active
    # does NOT block; exec proceeds to build the transport.
    cls, instance = mock_transport
    instance.exec.return_value = ExecResult(exit_code=0, stdout="ok")
    sb = make_sandbox(make_info(status=ReservationStatus.PREPARING))

    result = sb.exec("echo hi")
    assert result.stdout == "ok"
    cls.assert_called_once()


# ── upload / download ──────────────────────────────────────────────────────


def test_upload_delegates_to_transport(mock_transport):
    _cls, instance = mock_transport
    sb = make_sandbox()
    sb.upload("/local/a", "/remote/b")
    instance.upload.assert_called_once_with("/local/a", "/remote/b")


def test_download_delegates_to_transport(mock_transport):
    _cls, instance = mock_transport
    sb = make_sandbox()
    sb.download("/remote/b", "/local/a")
    instance.download.assert_called_once_with("/remote/b", "/local/a")


def test_upload_blocked_when_inactive(mock_transport):
    cls, _instance = mock_transport
    sb = make_sandbox(make_info(status=ReservationStatus.CANCELLED))
    with pytest.raises(GpuDevError):
        sb.upload("/a", "/b")
    cls.assert_not_called()


# ── cancel ─────────────────────────────────────────────────────────────────


def test_cancel_calls_backend_and_flips_status():
    backend = MagicMock()
    info = make_info()
    sb = make_sandbox(info, backend)

    sb.cancel()

    backend.cancel_reservation.assert_called_once_with(info.id, "user-1")
    assert sb.status == ReservationStatus.CANCELLED
    assert sb.is_active is False


def test_cancel_uses_full_id_not_prefix():
    backend = MagicMock()
    info = make_info(id="full-id-123456789")
    sb = make_sandbox(info, backend)
    sb.cancel()
    # passes the full reservation id, not the truncated repr form
    args, _ = backend.cancel_reservation.call_args
    assert args[0] == "full-id-123456789"


# ── extend / add_user (guarded by _ensure_active) ──────────────────────────


def test_extend_calls_backend_with_hours():
    backend = MagicMock()
    sb = make_sandbox(make_info(), backend)
    sb.extend(2.5)
    backend.extend_reservation.assert_called_once_with(sb.id, "user-1", 2.5)


def test_extend_blocked_when_inactive():
    backend = MagicMock()
    sb = make_sandbox(make_info(status=ReservationStatus.EXPIRED), backend)
    with pytest.raises(GpuDevError, match="expired"):
        sb.extend(1)
    backend.extend_reservation.assert_not_called()


def test_add_user_calls_backend():
    backend = MagicMock()
    sb = make_sandbox(make_info(), backend)
    sb.add_user("octocat")
    backend.add_user.assert_called_once_with(sb.id, "user-1", "octocat")


def test_add_user_blocked_when_inactive():
    backend = MagicMock()
    sb = make_sandbox(make_info(status=ReservationStatus.FAILED), backend)
    with pytest.raises(GpuDevError):
        sb.add_user("octocat")
    backend.add_user.assert_not_called()


# ── refresh ────────────────────────────────────────────────────────────────


def test_refresh_replaces_info_and_resets_transport(mock_transport):
    cls, instance = mock_transport
    instance.exec.return_value = ExecResult(exit_code=0)
    backend = MagicMock()
    sb = make_sandbox(make_info(), backend)
    sb.exec("warm up transport")  # build transport once
    assert cls.call_count == 1

    new_info = make_info(status=ReservationStatus.ACTIVE, detailed_status="updated")
    backend.poll_reservation_status.return_value = new_info

    sb.refresh()

    backend.poll_reservation_status.assert_called_once_with(sb.id)
    assert sb.info is new_info
    assert sb.detailed_status == "updated"
    # transport was reset -> next exec rebuilds it
    sb.exec("again")
    assert cls.call_count == 2


def test_refresh_noop_when_backend_returns_none():
    backend = MagicMock()
    backend.poll_reservation_status.return_value = None
    info = make_info()
    sb = make_sandbox(info, backend)
    sb.refresh()
    # info unchanged when poll returns falsy
    assert sb.info is info


# ── context manager auto-cancel ────────────────────────────────────────────


def test_context_manager_cancels_when_active():
    backend = MagicMock()
    sb = make_sandbox(make_info(status=ReservationStatus.ACTIVE), backend)
    with sb as entered:
        assert entered is sb
    backend.cancel_reservation.assert_called_once_with(sb.id, "user-1")
    assert sb.status == ReservationStatus.CANCELLED


def test_context_manager_no_cancel_when_inactive():
    backend = MagicMock()
    sb = make_sandbox(make_info(status=ReservationStatus.EXPIRED), backend)
    with sb:
        pass
    backend.cancel_reservation.assert_not_called()


def test_context_manager_cancels_even_on_exception():
    backend = MagicMock()
    sb = make_sandbox(make_info(status=ReservationStatus.ACTIVE), backend)
    with pytest.raises(ValueError):
        with sb:
            raise ValueError("boom")
    backend.cancel_reservation.assert_called_once()


# ── properties / repr ──────────────────────────────────────────────────────


def test_properties_proxy_info():
    info = make_info()
    sb = make_sandbox(info)
    assert sb.id == info.id
    assert sb.status == ReservationStatus.ACTIVE
    assert sb.gpu_type == "h100"
    assert sb.gpu_count == 2
    assert sb.name == "my-box"
    assert sb.pod_name == "gpu-dev-abc12345"
    assert sb.fqdn == "gpu-dev-abc12345.example.com"
    assert sb.ssh_command == "ssh dev@host"
    assert sb.expires_at == "2026-06-01T00:00:00"
    assert sb.jupyter_url == "http://jupyter"
    assert sb.disk_name == "default"
    assert sb.detailed_status == "running"
    assert sb.instance_type == "p5.48xlarge"
    assert sb.created_at == "2026-05-31T00:00:00"
    assert sb.user_id == "user-1"
    assert sb.info is info


def test_is_active_true_only_for_active_status():
    assert make_sandbox(make_info(status=ReservationStatus.ACTIVE)).is_active is True
    assert make_sandbox(make_info(status=ReservationStatus.PENDING)).is_active is False
    assert make_sandbox(make_info(status=ReservationStatus.CANCELLED)).is_active is False


def test_repr_truncates_id_and_shows_gpu():
    sb = make_sandbox(make_info(id="abc12345xyz", gpu_count=4, gpu_type="b200"))
    r = repr(sb)
    assert "abc12345" in r
    assert "abc12345xyz" not in r  # id truncated to 8 chars
    assert "status='active'" in r
    assert "gpu=4xb200" in r


# ── pod_logs (built on exec) ───────────────────────────────────────────────


def test_pod_logs_returns_stdout_and_uses_timeout(mock_transport):
    _cls, instance = mock_transport
    instance.exec.return_value = ExecResult(exit_code=0, stdout="line1\nline2\n")
    sb = make_sandbox()

    out = sb.pod_logs(lines=10)

    assert out == "line1\nline2\n"
    # pod_logs runs a tail command with timeout=10 and embeds the line count
    cmd, kwargs = instance.exec.call_args
    assert kwargs["timeout"] == 10
    assert "tail -10" in cmd[0]
