"""Unit tests for the AWS SDK backend (sdk/python/src/gpu_dev/_backend/aws.py).

All boto3 is mocked: ``_get_session`` is patched to return a MagicMock session,
so ``AwsBackend._init_clients`` wires mocked dynamodb resource / sqs / sts clients.
Per-table mocks are then attached directly to the instance (the real code calls
``.Table(...)`` three times on the same resource, so they share a mock by default).
No network, AWS, k8s, or subprocess.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

import gpu_dev
import gpu_dev._backend.aws as aws
from gpu_dev.common.config import GpuDevConfig
from gpu_dev.common.errors import GpuDevAuthError
from gpu_dev.common.models import DiskInfo, GpuAvailability, ReservationInfo


VERSION = gpu_dev.__version__


def make_backend(config: GpuDevConfig | None = None):
    """Build an AwsBackend with a fully-mocked boto3 session and distinct
    per-table mocks so tests can assert on each table independently."""
    config = config or GpuDevConfig(github_user="octocat", environment="prod")
    session = MagicMock(name="session")
    with patch.object(aws, "_get_session", return_value=session):
        backend = aws.AwsBackend(config)
    # Fresh session for the instance so constructor sqs/sts client() calls don't
    # bleed into per-test client-call assertions.
    backend._session = MagicMock(name="session_for_instance")
    backend._sqs = MagicMock(name="sqs")
    backend._sts = MagicMock(name="sts")
    backend._reservations = MagicMock(name="reservations_table")
    backend._availability = MagicMock(name="availability_table")
    backend._disks = MagicMock(name="disks_table")
    backend._queue_url = "https://sqs/queue"  # short-circuit get_queue_url
    return backend


def last_message(sqs_mock) -> dict:
    """Parse the JSON MessageBody of the most recent send_message call."""
    kwargs = sqs_mock.send_message.call_args.kwargs
    return json.loads(kwargs["MessageBody"])


# --------------------------------------------------------------------------- #
# construction / region resolution
# --------------------------------------------------------------------------- #
def test_region_from_environment_default():
    b = make_backend(GpuDevConfig(environment="prod"))
    assert b._region == "us-east-2"


def test_region_from_prod_east1_environment():
    b = make_backend(GpuDevConfig(environment="prod-east1"))
    assert b._region == "us-east-1"


def test_region_explicit_override_beats_environment():
    b = make_backend(GpuDevConfig(environment="prod", region="eu-west-1"))
    assert b._region == "eu-west-1"


def test_region_unknown_environment_falls_back_to_us_east_2():
    b = make_backend(GpuDevConfig(environment="does-not-exist"))
    assert b._region == "us-east-2"


def test_init_clients_uses_resolved_region_and_prefixed_tables():
    session = MagicMock(name="session")
    with patch.object(aws, "_get_session", return_value=session):
        aws.AwsBackend(GpuDevConfig(environment="prod"))
    session.resource.assert_called_with("dynamodb", region_name="us-east-2")
    session.client.assert_any_call("sqs", region_name="us-east-2")
    session.client.assert_any_call("sts", region_name="us-east-2")
    table_names = [c.args[0] for c in session.resource.return_value.Table.call_args_list]
    assert table_names == [
        "pytorch-gpu-dev-reservations",
        "pytorch-gpu-dev-gpu-availability",
        "pytorch-gpu-dev-disks",
    ]


# --------------------------------------------------------------------------- #
# _get_queue_url
# --------------------------------------------------------------------------- #
def test_get_queue_url_caches_after_first_call():
    b = make_backend()
    b._queue_url = None
    b._sqs.get_queue_url.return_value = {"QueueUrl": "https://q/abc"}
    assert b._get_queue_url() == "https://q/abc"
    assert b._get_queue_url() == "https://q/abc"
    b._sqs.get_queue_url.assert_called_once_with(
        QueueName="pytorch-gpu-dev-reservation-queue"
    )


# --------------------------------------------------------------------------- #
# authenticate
# --------------------------------------------------------------------------- #
def test_authenticate_returns_user_id_from_arn_and_github_user():
    b = make_backend(GpuDevConfig(github_user="octocat", environment="prod"))
    b._sts.get_caller_identity.return_value = {
        "Arn": "arn:aws:sts::123456789012:assumed-role/SomeRole/octocat",
    }
    out = b.authenticate()
    assert out == {"user_id": "octocat", "github_user": "octocat"}


def test_authenticate_empty_github_user_when_unset():
    b = make_backend(GpuDevConfig(environment="prod"))  # github_user None
    b._sts.get_caller_identity.return_value = {
        "Arn": "arn:aws:iam::123456789012:user/alice",
    }
    out = b.authenticate()
    assert out == {"user_id": "alice", "github_user": ""}


def test_authenticate_wraps_errors_in_auth_error():
    b = make_backend()
    b._sts.get_caller_identity.side_effect = RuntimeError("boom")
    with pytest.raises(GpuDevAuthError) as ei:
        b.authenticate()
    assert "Authentication failed" in str(ei.value)
    assert "boom" in str(ei.value)


# --------------------------------------------------------------------------- #
# _call (credential auto-refresh)
# --------------------------------------------------------------------------- #
def test_call_passes_through_return_value():
    b = make_backend()
    assert b._call(lambda: 42) == 42


def test_call_reraises_non_expired_client_error():
    b = make_backend()
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": "AccessDenied"}}, "GetCallerIdentity"
    )

    def fn():
        raise err

    with pytest.raises(botocore.exceptions.ClientError):
        b._call(fn)


def test_call_refreshes_and_retries_on_expired_token():
    b = make_backend()
    calls = {"n": 0}
    err = botocore.exceptions.ClientError(
        {"Error": {"Code": "ExpiredTokenException"}}, "GetCallerIdentity"
    )

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise err
        return "ok"

    with patch.object(b, "_refresh_on_expired") as refresh:
        assert b._call(fn) == "ok"
    refresh.assert_called_once()
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# create_reservation
# --------------------------------------------------------------------------- #
def test_create_reservation_sends_message_with_defaults_and_returns_uuid():
    b = make_backend()
    params = {"user_id": "alice"}
    with patch.object(aws.uuid, "uuid4", return_value="fixed-uuid"):
        rid = b.create_reservation(params)
    assert rid == "fixed-uuid"
    b._sqs.send_message.assert_called_once()
    msg = last_message(b._sqs)
    assert msg["reservation_id"] == "fixed-uuid"
    assert msg["user_id"] == "alice"
    assert msg["gpu_count"] == 1
    assert msg["gpu_type"] == "a100"
    assert msg["duration_hours"] == 8.0
    assert msg["status"] == "pending"
    assert msg["jupyter_enabled"] is False
    assert msg["no_persistent_disk"] is False
    assert msg["version"] == VERSION
    # optional keys absent when not supplied
    assert "disk_name" not in msg
    assert "dockerimage" not in msg
    assert "ref" not in msg
    assert "spot" not in msg


def test_create_reservation_threads_optional_fields():
    b = make_backend()
    params = {
        "user_id": "bob",
        "gpu_count": 4,
        "gpu_type": "h100",
        "duration_hours": 2.5,
        "name": "exp1",
        "jupyter": True,
        "recreate_env": True,
        "no_persistent_disk": True,
        "github_user": "bobgh",
        "preserve_entrypoint": True,
        "disk_name": "mydisk",
        "docker_image": "img:tag",
        "ref": "pr/123",
        "spot": True,
    }
    b.create_reservation(params)
    msg = last_message(b._sqs)
    assert msg["gpu_count"] == 4
    assert msg["gpu_type"] == "h100"
    assert msg["duration_hours"] == 2.5
    assert msg["name"] == "exp1"
    assert msg["jupyter_enabled"] is True
    assert msg["recreate_env"] is True
    assert msg["no_persistent_disk"] is True
    assert msg["github_user"] == "bobgh"
    assert msg["preserve_entrypoint"] is True
    assert msg["disk_name"] == "mydisk"
    # NOTE: the SQS key is "dockerimage", not "docker_image"
    assert msg["dockerimage"] == "img:tag"
    assert msg["ref"] == "pr/123"
    assert msg["spot"] is True


def test_create_reservation_uses_queue_url():
    b = make_backend()
    b._queue_url = "https://q/the-queue"
    b.create_reservation({"user_id": "alice"})
    assert b._sqs.send_message.call_args.kwargs["QueueUrl"] == "https://q/the-queue"


# --------------------------------------------------------------------------- #
# cancel / extend / add_user (all SQS control messages)
# --------------------------------------------------------------------------- #
def test_cancel_reservation_sends_cancellation_and_returns_true():
    b = make_backend()
    assert b.cancel_reservation("res-1", "alice") is True
    msg = last_message(b._sqs)
    assert msg == {
        "type": "cancellation",
        "reservation_id": "res-1",
        "user_id": "alice",
        "version": VERSION,
    }


def test_extend_reservation_sends_extend_with_hours():
    b = make_backend()
    assert b.extend_reservation("res-2", "bob", 3.5) is True
    msg = last_message(b._sqs)
    assert msg["type"] == "extend"
    assert msg["reservation_id"] == "res-2"
    assert msg["user_id"] == "bob"
    assert msg["extend_hours"] == 3.5
    assert msg["version"] == VERSION


def test_add_user_sends_add_user_message():
    b = make_backend()
    assert b.add_user("res-3", "owner", "collab-gh") is True
    msg = last_message(b._sqs)
    assert msg["type"] == "add_user"
    assert msg["reservation_id"] == "res-3"
    assert msg["user_id"] == "owner"
    assert msg["github_username"] == "collab-gh"


# --------------------------------------------------------------------------- #
# clone_disk / delete_disk
# --------------------------------------------------------------------------- #
def test_clone_disk_returns_operation_id_and_sends_message():
    b = make_backend()
    op_id = b.clone_disk("alice", "src", "dst")
    msg = last_message(b._sqs)
    assert msg["action"] == "clone_disk"
    assert msg["operation_id"] == op_id
    assert msg["user_id"] == "alice"
    assert msg["source_disk"] == "src"
    assert msg["target_disk"] == "dst"
    assert msg["version"] == VERSION
    assert "requested_at" in msg
    # operation id is a real uuid string
    assert len(op_id) == 36 and op_id.count("-") == 4


def test_delete_disk_returns_operation_id_and_sends_message():
    b = make_backend()
    op_id = b.delete_disk("alice", "olddisk")
    msg = last_message(b._sqs)
    assert msg["action"] == "delete_disk"
    assert msg["operation_id"] == op_id
    assert msg["user_id"] == "alice"
    assert msg["disk_name"] == "olddisk"
    assert "requested_at" in msg


# --------------------------------------------------------------------------- #
# get_reservation
# --------------------------------------------------------------------------- #
def test_get_reservation_full_id_uses_get_item():
    b = make_backend()
    full_id = "a" * 36
    b._reservations.get_item.return_value = {
        "Item": {"reservation_id": full_id, "status": "active", "gpu_count": 2}
    }
    info = b.get_reservation(full_id, "alice")
    b._reservations.get_item.assert_called_once_with(Key={"reservation_id": full_id})
    assert isinstance(info, ReservationInfo)
    assert info.id == full_id
    assert info.status == "active"
    assert info.gpu_count == 2


def test_get_reservation_full_id_missing_returns_none():
    b = make_backend()
    full_id = "b" * 40
    b._reservations.get_item.return_value = {}
    assert b.get_reservation(full_id, "alice") is None


def test_get_reservation_short_id_queries_user_index():
    b = make_backend()
    b._reservations.query.return_value = {
        "Items": [{"reservation_id": "abc12345", "status": "active"}]
    }
    info = b.get_reservation("abc", "alice")
    assert info is not None and info.id == "abc12345"
    kwargs = b._reservations.query.call_args.kwargs
    assert kwargs["IndexName"] == "UserIndex"
    assert kwargs["FilterExpression"] == "begins_with(reservation_id, :rid)"
    assert kwargs["ExpressionAttributeValues"] == {":uid": "alice", ":rid": "abc"}


def test_get_reservation_short_id_ambiguous_returns_none():
    b = make_backend()
    b._reservations.query.return_value = {
        "Items": [
            {"reservation_id": "abc1", "status": "active"},
            {"reservation_id": "abc2", "status": "active"},
        ]
    }
    assert b.get_reservation("abc", "alice") is None


def test_get_reservation_short_id_paginates_until_match():
    b = make_backend()
    b._reservations.query.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"k": "1"}},
        {"Items": [{"reservation_id": "abcZZ", "status": "queued"}]},
    ]
    info = b.get_reservation("abc", "alice")
    assert info is not None and info.id == "abcZZ"
    assert b._reservations.query.call_count == 2
    # second call must carry the ExclusiveStartKey from the first page
    second_kwargs = b._reservations.query.call_args_list[1].kwargs
    assert second_kwargs["ExclusiveStartKey"] == {"k": "1"}


def test_get_reservation_short_id_no_match_after_pages_returns_none():
    b = make_backend()
    b._reservations.query.side_effect = [
        {"Items": [], "LastEvaluatedKey": {"k": "1"}},
        {"Items": []},
    ]
    assert b.get_reservation("zzz", "alice") is None


# --------------------------------------------------------------------------- #
# list_reservations
# --------------------------------------------------------------------------- #
def test_list_reservations_empty_user_returns_empty():
    b = make_backend()
    assert b.list_reservations(user_id=None) == []
    b._reservations.query.assert_not_called()


def test_list_reservations_default_statuses_one_query_each():
    b = make_backend()
    b._reservations.query.return_value = {"Items": []}
    b.list_reservations(user_id="alice")
    assert b._reservations.query.call_count == 4
    statuses = [
        c.kwargs["ExpressionAttributeValues"][":status"]
        for c in b._reservations.query.call_args_list
    ]
    assert statuses == ["active", "pending", "queued", "preparing"]
    for c in b._reservations.query.call_args_list:
        assert c.kwargs["IndexName"] == "UserStatusIndex"
        assert c.kwargs["ExpressionAttributeNames"] == {"#s": "status"}
        assert c.kwargs["ExpressionAttributeValues"][":uid"] == "alice"


def test_list_reservations_custom_statuses():
    b = make_backend()
    b._reservations.query.return_value = {"Items": []}
    b.list_reservations(user_id="alice", statuses=["active"])
    assert b._reservations.query.call_count == 1
    assert (
        b._reservations.query.call_args.kwargs["ExpressionAttributeValues"][":status"]
        == "active"
    )


def test_list_reservations_sorts_by_created_at_desc():
    b = make_backend()
    b._reservations.query.side_effect = [
        {"Items": [{"reservation_id": "old", "status": "active",
                    "created_at": "2026-01-01T00:00:00"}]},
        {"Items": [{"reservation_id": "new", "status": "pending",
                    "created_at": "2026-05-01T00:00:00"}]},
        {"Items": []},
        {"Items": []},
    ]
    results = b.list_reservations(user_id="alice")
    assert [r.id for r in results] == ["new", "old"]


def test_list_reservations_handles_missing_created_at_in_sort():
    b = make_backend()
    b._reservations.query.side_effect = [
        {"Items": [{"reservation_id": "with", "status": "active",
                    "created_at": "2026-01-01T00:00:00"}]},
        {"Items": [{"reservation_id": "without", "status": "pending"}]},
        {"Items": []},
        {"Items": []},
    ]
    results = b.list_reservations(user_id="alice")
    # one with a timestamp sorts ahead of one without ("" sorts last desc)
    assert [r.id for r in results] == ["with", "without"]


# --------------------------------------------------------------------------- #
# get_availability
# --------------------------------------------------------------------------- #
def test_get_availability_parses_scan_items_with_decimals():
    b = make_backend()
    b._availability.scan.return_value = {
        "Items": [
            {
                "gpu_type": "h100",
                "available_gpus": Decimal("3"),
                "total_gpus": Decimal("8"),
                "max_reservable": Decimal("4"),
            },
            {
                "gpu_type": "t4",
                "available_gpus": Decimal("0"),
                "total_gpus": Decimal("4"),
            },
        ]
    }
    avail = b.get_availability()
    assert set(avail) == {"h100", "t4"}
    h = avail["h100"]
    assert isinstance(h, GpuAvailability)
    assert (h.available, h.total, h.max_reservable) == (3, 8, 4)
    # int coercion of Decimal, missing max_reservable defaults to 0
    assert isinstance(h.available, int)
    assert avail["t4"].max_reservable == 0


def test_get_availability_empty_scan_returns_empty_dict():
    b = make_backend()
    b._availability.scan.return_value = {}
    assert b.get_availability() == {}


# --------------------------------------------------------------------------- #
# list_disks
# --------------------------------------------------------------------------- #
def test_list_disks_maps_items_to_disk_info():
    b = make_backend()
    b._disks.query.return_value = {
        "Items": [
            {
                "disk_name": "d1",
                "size_gb": Decimal("100"),
                "snapshot_count": Decimal("2"),
                "in_use": True,
                "reservation_id": "res-9",
                "is_deleted": False,
            },
            {"disk_name": "d2"},  # all-default
        ]
    }
    disks = b.list_disks("alice")
    b._disks.query.assert_called_once_with(
        KeyConditionExpression="user_id = :uid",
        ExpressionAttributeValues={":uid": "alice"},
    )
    assert all(isinstance(d, DiskInfo) for d in disks)
    d1, d2 = disks
    assert (d1.name, d1.size_gb, d1.snapshot_count) == ("d1", 100, 2)
    assert d1.in_use is True
    assert d1.reservation_id == "res-9"
    assert d1.is_deleted is False
    # empty reservation_id -> None
    assert d2.name == "d2"
    assert d2.size_gb == 0
    assert d2.reservation_id is None
    assert d2.in_use is False


def test_list_disks_empty():
    b = make_backend()
    b._disks.query.return_value = {"Items": []}
    assert b.list_disks("alice") == []


# --------------------------------------------------------------------------- #
# poll_reservation_status
# --------------------------------------------------------------------------- #
def test_poll_reservation_status_found():
    b = make_backend()
    b._reservations.get_item.return_value = {
        "Item": {"reservation_id": "r1", "status": "active"}
    }
    info = b.poll_reservation_status("r1")
    assert info is not None and info.status == "active"
    b._reservations.get_item.assert_called_once_with(Key={"reservation_id": "r1"})


def test_poll_reservation_status_missing_returns_none():
    b = make_backend()
    b._reservations.get_item.return_value = {}
    assert b.poll_reservation_status("nope") is None


# --------------------------------------------------------------------------- #
# _item_to_info parsing
# --------------------------------------------------------------------------- #
def test_item_to_info_full_population():
    item = {
        "reservation_id": "r1",
        "status": "active",
        "gpu_type": "h100",
        "gpu_count": Decimal("8"),
        "name": "exp",
        "created_at": "2026-05-01T00:00:00",
        "launched_at": "2026-05-01T00:01:00",
        "expires_at": "2026-05-02T00:00:00",
        "ssh_command": "ssh pod",
        "pod_name": "pod-1",
        "fqdn": "pod.example.com",
        "node_ip": "10.0.0.1",
        "instance_type": "p5.48xlarge",
        "failure_reason": "",
        "current_detailed_status": "Booting",
        "jupyter_url": "http://jup",
        "jupyter_enabled": True,
        "disk_name": "d1",
        "is_multinode": True,
        "user_id": "alice",
    }
    info = aws.AwsBackend._item_to_info(item)
    assert info.id == "r1"
    assert info.gpu_count == 8
    assert isinstance(info.gpu_count, int)
    assert info.detailed_status == "Booting"
    assert info.jupyter_enabled is True
    assert info.is_multinode is True
    assert info.user_id == "alice"
    # empty failure_reason coerces to None
    assert info.failure_reason is None


def test_item_to_info_defaults_for_sparse_item():
    """A record missing 'status' defaults to PENDING (no ValidationError).

    Regression for the fix at aws.py: status was defaulted to 'unknown', which is
    not a ReservationStatus member and raised pydantic ValidationError on sparse
    DynamoDB items (records written before the status field exists).
    """
    info = aws.AwsBackend._item_to_info({})
    assert info.status == "pending"
    assert info.id == ""
    assert info.gpu_type == ""
    assert info.gpu_count == 1
    assert info.name is None
    assert info.jupyter_enabled is False
    assert info.is_multinode is False
    assert info.ssh_command is None


def test_item_to_info_defaults_with_explicit_status():
    """Sane defaults DO apply when status is a valid enum value."""
    info = aws.AwsBackend._item_to_info({"status": "pending"})
    assert info.id == ""
    assert info.status == "pending"
    assert info.gpu_type == ""
    assert info.gpu_count == 1
    assert info.name is None
    assert info.jupyter_enabled is False
    assert info.is_multinode is False
    assert info.ssh_command is None


# --------------------------------------------------------------------------- #
# _get_direct_url
# --------------------------------------------------------------------------- #
def test_get_direct_url_in_process_cache_short_circuits():
    b = make_backend()
    b._direct_url = "https://fn.url/cached"
    assert b._get_direct_url() == "https://fn.url/cached"
    # no lambda client call should happen
    b._session.client.assert_not_called()


def test_get_direct_url_in_process_empty_returns_none():
    b = make_backend()
    b._direct_url = ""
    assert b._get_direct_url() is None


def test_get_direct_url_fetches_from_lambda_when_no_cache(tmp_path):
    b = make_backend()
    lam = MagicMock(name="lambda")
    lam.get_function_url_config.return_value = {"FunctionUrl": "https://fn.url/live"}
    b._session.client.return_value = lam
    fake_cache = tmp_path / "direct-url.json"  # does not exist -> miss
    with patch.object(aws.Path, "home", return_value=tmp_path):
        # Path.home()/.config/... resolves under tmp_path; cache write is harmless
        url = b._get_direct_url()
    assert url == "https://fn.url/live"
    b._session.client.assert_called_once_with("lambda", region_name=b._region)
    lam.get_function_url_config.assert_called_once_with(
        FunctionName="pytorch-gpu-dev-reservation-processor", Qualifier="live"
    )


def test_get_direct_url_lambda_failure_returns_none(tmp_path):
    b = make_backend()
    lam = MagicMock(name="lambda")
    lam.get_function_url_config.side_effect = RuntimeError("denied")
    b._session.client.return_value = lam
    with patch.object(aws.Path, "home", return_value=tmp_path):
        assert b._get_direct_url() is None


# --------------------------------------------------------------------------- #
# _signed_post
# --------------------------------------------------------------------------- #
def test_signed_post_returns_parsed_json_on_200():
    b = make_backend()
    b._session.get_credentials.return_value = MagicMock(name="creds")
    resp_cm = MagicMock()
    resp_cm.status = 200
    resp_cm.read.return_value = b'{"claimed": true}'
    ctx = MagicMock()
    ctx.__enter__.return_value = resp_cm
    with patch.object(aws, "SigV4Auth"), \
         patch.object(aws.urllib.request, "urlopen", return_value=ctx):
        out = b._signed_post("https://fn.url", {"x": 1})
    assert out == {"claimed": True}


def test_signed_post_non_200_returns_none():
    b = make_backend()
    b._session.get_credentials.return_value = MagicMock(name="creds")
    resp_cm = MagicMock()
    resp_cm.status = 500
    ctx = MagicMock()
    ctx.__enter__.return_value = resp_cm
    with patch.object(aws, "SigV4Auth"), \
         patch.object(aws.urllib.request, "urlopen", return_value=ctx):
        assert b._signed_post("https://fn.url", {"x": 1}) is None


def test_signed_post_no_credentials_returns_none():
    b = make_backend()
    b._session.get_credentials.return_value = None
    assert b._signed_post("https://fn.url", {"x": 1}) is None


def test_signed_post_exception_returns_none():
    b = make_backend()
    b._session.get_credentials.side_effect = RuntimeError("nope")
    assert b._signed_post("https://fn.url", {"x": 1}) is None


# --------------------------------------------------------------------------- #
# claim_direct
# --------------------------------------------------------------------------- #
def test_claim_direct_no_url_sets_reason_and_returns_none():
    b = make_backend()
    with patch.object(b, "_get_direct_url", return_value=None):
        assert b.claim_direct({"user_id": "alice"}) is None
    assert b._direct_reason == "url_unavailable"


def test_claim_direct_request_failed_sets_reason():
    b = make_backend()
    with patch.object(b, "_get_direct_url", return_value="https://fn"), \
         patch.object(b, "_signed_post", return_value=None):
        assert b.claim_direct({"user_id": "alice"}) is None
    assert b._direct_reason == "request_failed"


def test_claim_direct_not_claimed_uses_reason_from_response():
    b = make_backend()
    with patch.object(b, "_get_direct_url", return_value="https://fn"), \
         patch.object(b, "_signed_post",
                      return_value={"claimed": False, "reason": "no_warm_pods"}):
        assert b.claim_direct({"user_id": "alice"}) is None
    assert b._direct_reason == "no_warm_pods"


def test_claim_direct_not_claimed_default_reason_unavailable():
    b = make_backend()
    with patch.object(b, "_get_direct_url", return_value="https://fn"), \
         patch.object(b, "_signed_post", return_value={"claimed": False}):
        assert b.claim_direct({"user_id": "alice"}) is None
    assert b._direct_reason == "unavailable"


def test_claim_direct_success_returns_reservation_payload():
    b = make_backend()
    reservation = {"reservation_id": "r1", "status": "active"}
    captured = {}

    def fake_post(url, payload):
        captured["payload"] = payload
        return {"claimed": True, "reservation": reservation}

    with patch.object(b, "_get_direct_url", return_value="https://fn"), \
         patch.object(b, "_signed_post", side_effect=fake_post):
        out = b.claim_direct({
            "user_id": "alice", "gpu_count": 2, "gpu_type": "h100",
            "duration_hours": 1.0, "ref": "pr/9", "github_user": "agh",
        })
    assert out == reservation
    p = captured["payload"]
    assert p["user_id"] == "alice"
    assert p["gpu_count"] == 2
    assert p["gpu_type"] == "h100"
    assert p["duration_hours"] == 1.0
    assert p["source_command"] == "reserve"
    assert p["ref"] == "pr/9"
    assert p["github_user"] == "agh"
    assert p["version"] == VERSION


def test_claim_direct_omits_ref_when_absent():
    b = make_backend()
    captured = {}

    def fake_post(url, payload):
        captured["payload"] = payload
        return {"claimed": True, "reservation": {}}

    with patch.object(b, "_get_direct_url", return_value="https://fn"), \
         patch.object(b, "_signed_post", side_effect=fake_post):
        b.claim_direct({"user_id": "alice"})
    assert "ref" not in captured["payload"]
    # defaults applied
    assert captured["payload"]["gpu_count"] == 1
    assert captured["payload"]["gpu_type"] == "a100"


# --------------------------------------------------------------------------- #
# _refresh_on_expired
# --------------------------------------------------------------------------- #
def test_refresh_on_expired_clears_module_cache_and_reinits():
    b = make_backend()
    aws._cached_session = MagicMock()
    aws._cached_session_expires = 9999999999.0
    new_session = MagicMock(name="new_session")
    with patch.object(aws, "_get_session", return_value=new_session), \
         patch.object(aws.Path, "unlink", create=True):
        # patch the path unlink on the module-level cred cache path
        with patch.object(type(aws._CRED_CACHE_PATH), "unlink", return_value=None):
            b._refresh_on_expired()
    assert aws._cached_session is None
    assert aws._cached_session_expires == 0
    assert b._queue_url is None
    # re-init wired clients from the fresh session
    new_session.resource.assert_called_with("dynamodb", region_name=b._region)
