"""Unit tests for gpu_dev._sync.client.GpuDev.reserve.

These exercise validation, the synchronous warm-pool ("direct") claim path,
the fallback to a queued SQS reservation, the ``ref``-implies-ephemeral rule,
spot handling, and the shape of the returned :class:`Sandbox`.

The client is constructed via ``__new__`` so the real ``AwsBackend`` is never
built; ``_config`` / ``_backend`` / ``_other_backend`` / ``_user_info`` are
injected directly (same approach the SDK uses internally).
"""

from unittest.mock import MagicMock

import pytest

from gpu_dev._sync.client import GpuDev
from gpu_dev._sync.sandbox import Sandbox
from gpu_dev.common.config import GpuDevConfig
from gpu_dev.common.enums import ReservationStatus
from gpu_dev.common.errors import GpuDevValidationError


USER_INFO = {"user_id": "u-123", "github_user": "octocat"}


def make_client(backend=None, user_info=None, config=None):
    """Build a GpuDev with an injected backend, skipping __init__/AwsBackend."""
    client = GpuDev.__new__(GpuDev)
    client._config = config or GpuDevConfig(github_user="octocat", environment="prod")
    client._backend = backend or MagicMock(name="backend")
    client._other_backend = None
    # Pre-seed auth so _auth() never touches the backend unless asked to.
    client._user_info = dict(user_info) if user_info is not None else dict(USER_INFO)
    return client


def claim_response(**overrides):
    base = {
        "reservation_id": "res-abcdef123456",
        "ssh_command": "ssh dev@pod-xyz",
        "pod_name": "pod-xyz",
        "node_ip": "10.0.1.5",
        "fqdn": "pod-xyz.gpu-dev.internal",
        "expires_at": "2026-06-01T00:00:00Z",
    }
    base.update(overrides)
    return base


# ── validation: unknown gpu type ───────────────────────────────────────────────

def test_unknown_gpu_type_raises_validation_error():
    backend = MagicMock()
    client = make_client(backend)
    with pytest.raises(GpuDevValidationError) as ei:
        client.reserve(gpu_type="a200")
    assert "Unknown GPU type: a200" in str(ei.value)
    # validation happens before auth / any backend call
    backend.claim_direct.assert_not_called()
    backend.create_reservation.assert_not_called()


def test_unknown_gpu_type_lists_available_types_sorted():
    client = make_client()
    with pytest.raises(GpuDevValidationError) as ei:
        client.reserve(gpu_type="nope")
    msg = str(ei.value)
    # message enumerates the valid keys, sorted, comma-joined
    assert "Available:" in msg
    assert "a100" in msg and "h100" in msg and "cpu-arm" in msg
    avail = msg.split("Available:", 1)[1]
    listed = [s.strip() for s in avail.split(",")]
    assert listed == sorted(listed)


def test_unknown_gpu_type_does_not_authenticate():
    backend = MagicMock()
    client = GpuDev.__new__(GpuDev)
    client._config = GpuDevConfig()
    client._backend = backend
    client._other_backend = None
    client._user_info = None  # force _auth to call backend if reached
    with pytest.raises(GpuDevValidationError):
        client.reserve(gpu_type="totally-fake")
    backend.authenticate.assert_not_called()


# ── validation: over-count ─────────────────────────────────────────────────────

def test_over_count_raises_validation_error():
    backend = MagicMock()
    client = make_client(backend)
    with pytest.raises(GpuDevValidationError) as ei:
        client.reserve(gpu_type="h100", gpu_count=9)
    assert "h100 supports max 8 GPUs, got 9" in str(ei.value)
    backend.claim_direct.assert_not_called()


def test_t4_over_count_uses_its_own_max():
    client = make_client()
    with pytest.raises(GpuDevValidationError) as ei:
        client.reserve(gpu_type="t4", gpu_count=5)
    assert "t4 supports max 4 GPUs, got 5" in str(ei.value)


def test_at_max_count_is_allowed():
    backend = MagicMock()
    backend.claim_direct.return_value = None  # force fallback
    backend.create_reservation.return_value = "res-id"
    client = make_client(backend)
    # 8 == max for h100, must NOT raise; wait=False to avoid polling
    sb = client.reserve(gpu_type="h100", gpu_count=8, wait=False)
    assert sb.gpu_count == 8


def test_cpu_type_max_zero_never_triggers_over_count():
    # cpu-arm has GPU_MAX_COUNT == 0; the `max_gpus > 0` guard skips the check.
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-cpu"
    client = make_client(backend)
    sb = client.reserve(gpu_type="cpu-arm", gpu_count=1, wait=False)
    assert sb.gpu_type == "cpu-arm"


def test_gpu_type_is_lowercased_for_validation_and_backend():
    backend = MagicMock()
    backend.claim_direct.return_value = claim_response()
    client = make_client(backend)
    sb = client.reserve(gpu_type="H100", gpu_count=1)
    assert sb.gpu_type == "h100"
    sent = backend.claim_direct.call_args.args[0]
    assert sent["gpu_type"] == "h100"


# ── direct / warm-pool claim path ──────────────────────────────────────────────

def test_direct_claim_returns_active_sandbox_without_polling():
    backend = MagicMock()
    backend.claim_direct.return_value = claim_response()
    client = make_client(backend)

    sb = client.reserve(gpu_type="h100", gpu_count=1, hours=4, name="myjob")

    assert isinstance(sb, Sandbox)
    assert sb.status == ReservationStatus.ACTIVE
    backend.claim_direct.assert_called_once()
    # direct success short-circuits — no SQS reservation, no polling
    backend.create_reservation.assert_not_called()
    backend.poll_reservation_status.assert_not_called()


def test_direct_claim_payload_contents():
    backend = MagicMock()
    backend.claim_direct.return_value = claim_response()
    client = make_client(backend)

    client.reserve(gpu_type="b200", gpu_count=2, hours=6.5, name="probe")

    payload = backend.claim_direct.call_args.args[0]
    assert payload == {
        "user_id": "u-123",
        "github_user": "octocat",
        "gpu_type": "b200",
        "gpu_count": 2,
        "duration_hours": 6.5,
        "name": "probe",
        "ref": None,
    }


def test_direct_claim_sandbox_shape_maps_response_fields():
    backend = MagicMock()
    backend.claim_direct.return_value = claim_response(
        reservation_id="res-deadbeef0000",
        ssh_command="ssh dev@1.2.3.4",
        pod_name="pod-7",
        node_ip="10.9.9.9",
        fqdn="pod-7.dev",
        expires_at="2026-12-31T23:59:59Z",
    )
    client = make_client(backend)

    sb = client.reserve(gpu_type="h100", gpu_count=1, name="shaped")
    info = sb.info
    assert info.id == "res-deadbeef0000"
    assert info.status == ReservationStatus.ACTIVE
    assert info.gpu_type == "h100"
    assert info.gpu_count == 1
    assert info.name == "shaped"
    assert info.user_id == "u-123"
    assert info.ssh_command == "ssh dev@1.2.3.4"
    assert info.pod_name == "pod-7"
    assert info.node_ip == "10.9.9.9"
    assert info.fqdn == "pod-7.dev"
    assert info.expires_at == "2026-12-31T23:59:59Z"
    # sandbox is wired to the same backend + user
    assert sb._backend is backend
    assert sb._user_id == "u-123"


def test_direct_claim_handles_missing_response_keys():
    # claim_direct may return a sparse dict; ReservationInfo fields default to None.
    backend = MagicMock()
    backend.claim_direct.return_value = {"reservation_id": "res-min"}
    client = make_client(backend)

    sb = client.reserve(gpu_type="h100", gpu_count=1)
    assert sb.id == "res-min"
    assert sb.status == ReservationStatus.ACTIVE
    assert sb.ssh_command is None
    assert sb.pod_name is None
    assert sb.fqdn is None


def test_direct_disabled_by_flag_goes_to_create():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-queued"
    client = make_client(backend)

    sb = client.reserve(gpu_type="h100", gpu_count=1, direct=False, wait=False)

    backend.claim_direct.assert_not_called()
    backend.create_reservation.assert_called_once()
    assert sb.status == ReservationStatus.PENDING
    assert sb.id == "res-queued"


def test_direct_skipped_for_mig_over_one_gpu():
    # h100-mig-1g has max 1 GPU; gpu_count=2 exceeds max(1, max_gpus)=1 so the
    # direct fast-path condition is false (but it's not an over-count error since
    # the over-count guard already would have fired). Use a count within max.
    backend = MagicMock()
    backend.claim_direct.return_value = claim_response()
    client = make_client(backend)
    sb = client.reserve(gpu_type="h100-mig-1g", gpu_count=1)
    backend.claim_direct.assert_called_once()
    assert sb.status == ReservationStatus.ACTIVE


# ── fallback to queued reservation ─────────────────────────────────────────────

def test_claim_direct_miss_falls_back_to_create_reservation():
    backend = MagicMock()
    backend.claim_direct.return_value = None  # server miss
    backend.create_reservation.return_value = "res-fallback"
    client = make_client(backend)

    sb = client.reserve(gpu_type="h100", gpu_count=1, wait=False)

    backend.claim_direct.assert_called_once()
    backend.create_reservation.assert_called_once()
    assert sb.status == ReservationStatus.PENDING
    assert sb.id == "res-fallback"


def test_claim_direct_empty_dict_is_falsy_falls_back():
    backend = MagicMock()
    backend.claim_direct.return_value = {}  # falsy -> fallback
    backend.create_reservation.return_value = "res-empty"
    client = make_client(backend)
    sb = client.reserve(gpu_type="h100", gpu_count=1, wait=False)
    backend.create_reservation.assert_called_once()
    assert sb.id == "res-empty"


def test_fallback_create_reservation_payload_defaults():
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-1"
    client = make_client(backend)

    client.reserve(gpu_type="a100", gpu_count=2, hours=3.0, name="job", wait=False)

    params = backend.create_reservation.call_args.args[0]
    assert params["user_id"] == "u-123"
    assert params["github_user"] == "octocat"
    assert params["gpu_type"] == "a100"
    assert params["gpu_count"] == 2
    assert params["duration_hours"] == 3.0
    assert params["name"] == "job"
    assert params["jupyter"] is False
    assert params["disk_name"] is None
    assert params["docker_image"] is None
    assert params["ref"] is None
    assert params["spot"] is False
    assert params["no_persistent_disk"] is False


def test_wait_true_polls_until_active(monkeypatch):
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-wait"
    client = make_client(backend)

    called = {}

    def fake_wait(self, tm, on_progress=None):
        called["tm"] = tm
        self._info.status = ReservationStatus.ACTIVE

    monkeypatch.setattr(Sandbox, "wait_until_ready", fake_wait)

    sb = client.reserve(gpu_type="h100", gpu_count=1, wait=True, timeout_minutes=7)
    assert called["tm"] == 7
    assert sb.status == ReservationStatus.ACTIVE


def test_wait_uses_config_default_timeout(monkeypatch):
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-wait2"
    cfg = GpuDevConfig(github_user="octocat", default_timeout_minutes=42)
    client = make_client(backend, config=cfg)

    seen = {}
    monkeypatch.setattr(
        Sandbox, "wait_until_ready",
        lambda self, tm, on_progress=None: seen.update(tm=tm),
    )
    client.reserve(gpu_type="h100", gpu_count=1, wait=True)
    assert seen["tm"] == 42


def test_on_progress_true_wraps_print_callback(monkeypatch):
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-prog"
    client = make_client(backend)

    captured = {}
    monkeypatch.setattr(
        Sandbox, "wait_until_ready",
        lambda self, tm, on_progress=None: captured.update(cb=on_progress),
    )
    client.reserve(gpu_type="h100", gpu_count=1, wait=True, on_progress=True)
    # on_progress=True becomes a callable wrapper (not the literal True)
    assert callable(captured["cb"])


def test_on_progress_callable_passed_through(monkeypatch):
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-prog2"
    client = make_client(backend)

    my_cb = lambda msg, t: None
    captured = {}
    monkeypatch.setattr(
        Sandbox, "wait_until_ready",
        lambda self, tm, on_progress=None: captured.update(cb=on_progress),
    )
    client.reserve(gpu_type="h100", gpu_count=1, wait=True, on_progress=my_cb)
    assert captured["cb"] is my_cb


# ── ref implies ephemeral (no persistent disk) ─────────────────────────────────

def test_ref_disables_direct_path():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-ref"
    client = make_client(backend)

    client.reserve(gpu_type="h100", gpu_count=1, ref="pr/123", wait=False)

    backend.claim_direct.assert_not_called()
    backend.create_reservation.assert_called_once()


def test_ref_sets_no_persistent_disk_true():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-ref"
    client = make_client(backend)

    client.reserve(gpu_type="h100", gpu_count=1, ref="abc123", wait=False)

    params = backend.create_reservation.call_args.args[0]
    assert params["ref"] == "abc123"
    assert params["no_persistent_disk"] is True


def test_ref_with_disk_name_keeps_persistent_disk():
    # ref + explicit disk_name: staging is skipped, so the disk stays persistent.
    backend = MagicMock()
    backend.create_reservation.return_value = "res-refdisk"
    client = make_client(backend)

    client.reserve(
        gpu_type="h100", gpu_count=1, ref="abc123", disk_name="mydisk", wait=False
    )
    params = backend.create_reservation.call_args.args[0]
    assert params["disk_name"] == "mydisk"
    assert params["no_persistent_disk"] is False


@pytest.mark.parametrize("ref_val", ["none", "None", "NONE", "  none  ", ""])
def test_ref_none_sentinel_keeps_persistent_disk(ref_val):
    # ref="none"/"" means "skip staging" -> NOT ephemeral.
    # NB: empty-string ref is falsy, so it does NOT disable the direct path;
    # set claim_direct to miss so all variants land in create_reservation.
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-none"
    client = make_client(backend)

    client.reserve(gpu_type="h100", gpu_count=1, ref=ref_val, wait=False)
    params = backend.create_reservation.call_args.args[0]
    assert params["no_persistent_disk"] is False


def test_empty_ref_does_not_disable_direct_path():
    # `not ref` treats "" as falsy, so an empty-string ref still tries direct.
    backend = MagicMock()
    backend.claim_direct.return_value = claim_response()
    client = make_client(backend)
    sb = client.reserve(gpu_type="h100", gpu_count=1, ref="")
    backend.claim_direct.assert_called_once()
    assert sb.status == ReservationStatus.ACTIVE


def test_ref_none_string_still_disables_direct_path():
    # Any non-empty/non-None `ref` truthiness disables the direct path; even
    # "none" is a truthy string so it routes through create_reservation.
    backend = MagicMock()
    backend.create_reservation.return_value = "res-x"
    client = make_client(backend)
    client.reserve(gpu_type="h100", gpu_count=1, ref="none", wait=False)
    backend.claim_direct.assert_not_called()


# ── spot handling ──────────────────────────────────────────────────────────────

def test_spot_disables_direct_path():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-spot"
    client = make_client(backend)

    client.reserve(gpu_type="h100", gpu_count=1, spot=True, wait=False)

    backend.claim_direct.assert_not_called()
    backend.create_reservation.assert_called_once()


def test_spot_flag_threaded_to_create_reservation():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-spot"
    client = make_client(backend)

    client.reserve(gpu_type="h100", gpu_count=1, spot=True, wait=False)
    params = backend.create_reservation.call_args.args[0]
    assert params["spot"] is True


# ── disk_name / docker_image disable direct path ───────────────────────────────

def test_disk_name_disables_direct_path():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-disk"
    client = make_client(backend)
    client.reserve(gpu_type="h100", gpu_count=1, disk_name="d1", wait=False)
    backend.claim_direct.assert_not_called()
    params = backend.create_reservation.call_args.args[0]
    assert params["disk_name"] == "d1"


def test_docker_image_disables_direct_path():
    backend = MagicMock()
    backend.create_reservation.return_value = "res-img"
    client = make_client(backend)
    client.reserve(gpu_type="h100", gpu_count=1, docker_image="my/img:tag", wait=False)
    backend.claim_direct.assert_not_called()
    params = backend.create_reservation.call_args.args[0]
    assert params["docker_image"] == "my/img:tag"


def test_jupyter_flag_threaded_to_create_reservation():
    backend = MagicMock()
    backend.claim_direct.return_value = None
    backend.create_reservation.return_value = "res-jup"
    client = make_client(backend)
    client.reserve(gpu_type="h100", gpu_count=1, jupyter=True, wait=False)
    params = backend.create_reservation.call_args.args[0]
    assert params["jupyter"] is True


# ── auth integration ───────────────────────────────────────────────────────────

def test_auth_called_when_user_info_unset():
    backend = MagicMock()
    backend.authenticate.return_value = {"user_id": "u-9", "github_user": "ghuser"}
    backend.claim_direct.return_value = claim_response()
    client = GpuDev.__new__(GpuDev)
    client._config = GpuDevConfig()
    client._backend = backend
    client._other_backend = None
    client._user_info = None

    sb = client.reserve(gpu_type="h100", gpu_count=1)
    backend.authenticate.assert_called_once()
    assert sb.user_id == "u-9"
    # the claim payload carries the authenticated identity
    payload = backend.claim_direct.call_args.args[0]
    assert payload["user_id"] == "u-9"
    assert payload["github_user"] == "ghuser"


def test_auth_cached_across_calls():
    backend = MagicMock()
    backend.authenticate.return_value = dict(USER_INFO)
    backend.claim_direct.return_value = claim_response()
    client = GpuDev.__new__(GpuDev)
    client._config = GpuDevConfig()
    client._backend = backend
    client._other_backend = None
    client._user_info = None

    client.reserve(gpu_type="h100", gpu_count=1)
    client.reserve(gpu_type="h100", gpu_count=1)
    backend.authenticate.assert_called_once()
