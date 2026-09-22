"""Unit tests for `ReservationManager` (gpu_dev_cli/reservations.py).

These exercise the manager methods directly (not the click commands), asserting
the *exact shape* of the SQS messages / Function-URL payloads they emit, the
DynamoDB query/scan branches in `get_connection_info`, and the Decimal->int
coercion + pagination in `get_gpu_availability_by_type`.

Everything external is mocked: the `Config` is a `MagicMock`, so
`config.dynamodb.Table(...)`, `config.sqs_client`, `config.session` and
`config.get_queue_url()` are all MagicMocks we set up / assert against. `requests`
is patched inside `gpu_dev_cli.reservations` for the Function-URL path. No
network, AWS, or filesystem dependency.
"""
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from gpu_dev_cli.reservations import ReservationManager, get_version


QUEUE_URL = "https://sqs.us-east-2.amazonaws.com/000000000000/test-queue"


def _make_config():
    """A MagicMock Config wired so ReservationManager.__init__ succeeds and
    every method has a deterministic queue URL / table handle."""
    cfg = MagicMock(name="config")
    cfg.reservations_table = "pytorch-gpu-dev-reservations"
    cfg.availability_table = "pytorch-gpu-dev-gpu-availability"
    cfg.aws_region = "us-east-2"
    cfg.prefix = "pytorch-gpu-dev"
    cfg.get_queue_url.return_value = QUEUE_URL
    # ReservationManager.__init__ does config.dynamodb.Table(config.reservations_table)
    cfg._reservations_table_mock = MagicMock(name="reservations_table")
    cfg.dynamodb.Table.return_value = cfg._reservations_table_mock
    return cfg


def _make_mgr():
    cfg = _make_config()
    mgr = ReservationManager(cfg)
    # Convenience handles
    mgr._cfg = cfg
    mgr._table = cfg._reservations_table_mock
    return mgr


def _sent_message(cfg):
    """Parse the JSON body of the single send_message call on the config's sqs."""
    cfg.sqs_client.send_message.assert_called_once()
    _, kwargs = cfg.sqs_client.send_message.call_args
    return json.loads(kwargs["MessageBody"]), kwargs


# --------------------------------------------------------------------------- #
# __init__                                                                     #
# --------------------------------------------------------------------------- #
def test_init_builds_table_from_config():
    cfg = _make_config()
    mgr = ReservationManager(cfg)
    cfg.dynamodb.Table.assert_called_once_with("pytorch-gpu-dev-reservations")
    assert mgr.reservations_table is cfg._reservations_table_mock
    assert mgr.config is cfg


# --------------------------------------------------------------------------- #
# cancel_reservation — SQS message shape                                       #
# --------------------------------------------------------------------------- #
def test_cancel_reservation_message_shape():
    mgr = _make_mgr()
    rid = "5e83bb5b-aaaa-bbbb-cccc-1234567890ab"
    ok = mgr.cancel_reservation(rid, "octocat")

    assert ok is True
    body, kwargs = _sent_message(mgr._cfg)
    assert kwargs["QueueUrl"] == QUEUE_URL
    assert body["type"] == "cancellation"
    assert body["reservation_id"] == rid
    assert body["user_id"] == "octocat"
    assert body["version"] == get_version()
    # requested_at is an ISO timestamp string
    assert isinstance(body["requested_at"], str) and "T" in body["requested_at"]
    # No stray keys beyond the documented contract.
    assert set(body) == {"type", "reservation_id", "user_id", "requested_at", "version"}


def test_cancel_reservation_returns_false_on_sqs_error():
    mgr = _make_mgr()
    mgr._cfg.sqs_client.send_message.side_effect = RuntimeError("sqs down")
    assert mgr.cancel_reservation("rid", "uid") is False


def test_cancel_reservation_returns_false_on_queue_url_error():
    mgr = _make_mgr()
    mgr._cfg.get_queue_url.side_effect = RuntimeError("no perms")
    assert mgr.cancel_reservation("rid", "uid") is False
    mgr._cfg.sqs_client.send_message.assert_not_called()


# --------------------------------------------------------------------------- #
# create_reservation — SQS message shape + options                            #
# --------------------------------------------------------------------------- #
def test_create_reservation_basic_message_shape():
    mgr = _make_mgr()
    rid = mgr.create_reservation(
        user_id="octocat", gpu_count=2, gpu_type="h100",
        duration_hours=4, github_user="octocat",
    )
    assert rid is not None and len(rid) == 36  # uuid4
    body, kwargs = _sent_message(mgr._cfg)
    assert kwargs["QueueUrl"] == QUEUE_URL
    assert body["reservation_id"] == rid
    assert body["user_id"] == "octocat"
    assert body["gpu_count"] == 2
    assert body["gpu_type"] == "h100"
    assert body["duration_hours"] == 4.0  # float-coerced for JSON
    assert body["status"] == "pending"
    assert body["github_user"] == "octocat"
    assert body["version"] == get_version()
    assert body["source_command"] == "reserve"  # default
    # preserve_entrypoint is always present (not conditional)
    assert body["preserve_entrypoint"] is False
    assert body["no_persistent_disk"] is False
    assert body["jupyter_enabled"] is False
    assert body["recreate_env"] is False


def test_create_reservation_float_hours_serialized_as_float():
    mgr = _make_mgr()
    mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                           duration_hours=0.25)
    body, _ = _sent_message(mgr._cfg)
    assert body["duration_hours"] == 0.25
    assert isinstance(body["duration_hours"], float)


def test_create_reservation_sanitizes_name():
    mgr = _make_mgr()
    with patch("gpu_dev_cli.reservations.sanitize_name",
               return_value="clean-name") as san:
        mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                               duration_hours=1, name="My Messy Name!!")
    san.assert_called_once_with("My Messy Name!!")
    body, _ = _sent_message(mgr._cfg)
    assert body["name"] == "clean-name"


def test_create_reservation_empty_sanitized_name_becomes_none():
    """If sanitize_name returns falsy, the message carries name=None so the
    Lambda generates one."""
    mgr = _make_mgr()
    with patch("gpu_dev_cli.reservations.sanitize_name", return_value=""):
        mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                               duration_hours=1, name="!!!")
    body, _ = _sent_message(mgr._cfg)
    assert body["name"] is None


def test_create_reservation_no_name_is_none():
    mgr = _make_mgr()
    mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                           duration_hours=1)
    body, _ = _sent_message(mgr._cfg)
    assert body["name"] is None
    # github_user omitted entirely when not provided
    assert "github_user" not in body


def test_create_reservation_optional_fields_threaded():
    mgr = _make_mgr()
    mgr.create_reservation(
        user_id="u", gpu_count=8, gpu_type="b200", duration_hours=2,
        github_user="gh", jupyter_enabled=True, recreate_env=True,
        dockerfile="FROM x", dockerimage="img:latest",
        no_persistent_disk=True, preserve_entrypoint=True,
        disk_name="mydisk", node_labels={"zone": "a"},
        spot=True, fast_cache=True, source_command="test", ref="pr/123",
    )
    body, _ = _sent_message(mgr._cfg)
    assert body["jupyter_enabled"] is True
    assert body["recreate_env"] is True
    assert body["dockerfile"] == "FROM x"
    assert body["dockerimage"] == "img:latest"
    assert body["no_persistent_disk"] is True
    assert body["preserve_entrypoint"] is True
    assert body["disk_name"] == "mydisk"
    assert body["node_labels"] == {"zone": "a"}
    assert body["spot"] is True
    assert body["fast_cache"] is True
    assert body["source_command"] == "test"
    assert body["ref"] == "pr/123"
    assert body["github_user"] == "gh"


def test_create_reservation_omits_unset_optionals():
    """spot/fast_cache/ref/dockerfile/dockerimage/disk_name/node_labels are only
    present when truthy."""
    mgr = _make_mgr()
    mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                           duration_hours=1)
    body, _ = _sent_message(mgr._cfg)
    for absent in ("spot", "fast_cache", "ref", "dockerfile", "dockerimage",
                   "disk_name", "node_labels", "trace"):
        assert absent not in body


def test_create_reservation_trace_adds_flag_and_timestamp(capsys):
    mgr = _make_mgr()
    mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                           duration_hours=1, trace=True)
    body, _ = _sent_message(mgr._cfg)
    assert body["trace"] is True
    assert isinstance(body["trace_cli_start"], float)


def test_create_reservation_returns_none_on_error():
    mgr = _make_mgr()
    mgr._cfg.sqs_client.send_message.side_effect = RuntimeError("boom")
    assert mgr.create_reservation(user_id="u", gpu_count=1, gpu_type="t4",
                                  duration_hours=1) is None


# --------------------------------------------------------------------------- #
# claim_direct — Function URL payload                                          #
# --------------------------------------------------------------------------- #
def test_claim_direct_url_unavailable_returns_none():
    mgr = _make_mgr()
    with patch.object(mgr, "_get_direct_url", return_value=None):
        result = mgr.claim_direct(user_id="u", gpu_count=1, gpu_type="h100",
                                  duration_hours=1)
    assert result is None
    assert mgr._direct_reason == "url_unavailable"


def test_claim_direct_request_failed_when_post_none():
    mgr = _make_mgr()
    with patch.object(mgr, "_get_direct_url", return_value="https://fn.url/"), \
         patch.object(mgr, "_signed_post", return_value=None):
        result = mgr.claim_direct(user_id="u", gpu_count=1, gpu_type="h100",
                                  duration_hours=1)
    assert result is None
    assert mgr._direct_reason == "request_failed"


def test_claim_direct_not_claimed_records_reason():
    mgr = _make_mgr()
    with patch.object(mgr, "_get_direct_url", return_value="https://fn.url/"), \
         patch.object(mgr, "_signed_post",
                      return_value={"claimed": False, "reason": "no_warm_pod"}):
        result = mgr.claim_direct(user_id="u", gpu_count=1, gpu_type="h100",
                                  duration_hours=1)
    assert result is None
    assert mgr._direct_reason == "no_warm_pod"


def test_claim_direct_not_claimed_default_reason_unavailable():
    mgr = _make_mgr()
    with patch.object(mgr, "_get_direct_url", return_value="https://fn.url/"), \
         patch.object(mgr, "_signed_post", return_value={"claimed": False}):
        result = mgr.claim_direct(user_id="u", gpu_count=1, gpu_type="h100",
                                  duration_hours=1)
    assert result is None
    assert mgr._direct_reason == "unavailable"


def test_claim_direct_success_returns_reservation_and_payload_shape():
    mgr = _make_mgr()
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"claimed": True, "reservation": {"reservation_id": "r1", "status": "active"}}

    with patch.object(mgr, "_get_direct_url", return_value="https://fn.url/"), \
         patch.object(mgr, "_signed_post", side_effect=fake_post):
        result = mgr.claim_direct(user_id="octocat", gpu_count=2, gpu_type="h100",
                                  duration_hours=3, name="my-box",
                                  github_user="octocat", ref="pr/9")

    assert result == {"reservation_id": "r1", "status": "active"}
    p = captured["payload"]
    assert captured["url"] == "https://fn.url/"
    assert p["user_id"] == "octocat"
    assert p["gpu_count"] == 2
    assert p["gpu_type"] == "h100"
    assert p["duration_hours"] == 3.0
    assert p["name"] == "my-box"
    assert p["github_user"] == "octocat"
    assert p["status"] == "pending"
    assert p["source_command"] == "reserve"
    assert p["version"] == get_version()
    assert p["ref"] == "pr/9"
    assert len(p["reservation_id"]) == 36  # fresh uuid4
    assert mgr._direct_reason is None


def test_claim_direct_omits_ref_when_not_given_and_empty_github():
    mgr = _make_mgr()
    captured = {}
    with patch.object(mgr, "_get_direct_url", return_value="https://fn.url/"), \
         patch.object(mgr, "_signed_post",
                      side_effect=lambda url, payload: captured.update(payload) or
                      {"claimed": True, "reservation": {}}):
        mgr.claim_direct(user_id="u", gpu_count=1, gpu_type="h100",
                         duration_hours=1)
    assert "ref" not in captured
    assert captured["github_user"] == ""  # defaults to "" not None


def test_claim_direct_claimed_true_but_no_reservation_returns_none():
    """claimed True with a missing reservation key -> .get returns None."""
    mgr = _make_mgr()
    with patch.object(mgr, "_get_direct_url", return_value="https://fn.url/"), \
         patch.object(mgr, "_signed_post", return_value={"claimed": True}):
        result = mgr.claim_direct(user_id="u", gpu_count=1, gpu_type="h100",
                                  duration_hours=1)
    assert result is None
    # reason stays None because it WAS claimed
    assert mgr._direct_reason is None


# --------------------------------------------------------------------------- #
# _signed_post — SigV4 + requests                                             #
# --------------------------------------------------------------------------- #
def test_signed_post_returns_none_without_credentials():
    mgr = _make_mgr()
    mgr._cfg.session.get_credentials.return_value = None
    with patch("gpu_dev_cli.reservations.requests") as req:
        assert mgr._signed_post("https://fn.url/", {"a": 1}) is None
        req.post.assert_not_called()


def test_signed_post_non_200_returns_none():
    mgr = _make_mgr()
    mgr._cfg.session.get_credentials.return_value = MagicMock()
    resp = MagicMock(status_code=403)
    with patch("gpu_dev_cli.reservations.requests") as req, \
         patch("gpu_dev_cli.reservations.SigV4Auth"):
        req.post.return_value = resp
        assert mgr._signed_post("https://fn.url/", {"a": 1}) is None


def test_signed_post_success_returns_json():
    mgr = _make_mgr()
    mgr._cfg.session.get_credentials.return_value = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"claimed": True}
    with patch("gpu_dev_cli.reservations.requests") as req, \
         patch("gpu_dev_cli.reservations.SigV4Auth"):
        req.post.return_value = resp
        out = mgr._signed_post("https://fn.url/", {"a": 1})
    assert out == {"claimed": True}
    # The POSTed body is the JSON-serialized payload
    _, kwargs = req.post.call_args
    assert json.loads(kwargs["data"]) == {"a": 1}
    assert kwargs["timeout"] == 20


def test_signed_post_swallows_exceptions():
    mgr = _make_mgr()
    mgr._cfg.session.get_credentials.side_effect = RuntimeError("creds boom")
    assert mgr._signed_post("https://fn.url/", {"a": 1}) is None


# --------------------------------------------------------------------------- #
# get_connection_info                                                          #
# --------------------------------------------------------------------------- #
def _single_reservation(**overrides):
    base = {
        "reservation_id": "abc12345-1111-2222-3333-444455556666",
        "user_id": "octocat",
        "gpu_count": Decimal("2"),
        "status": "active",
        "pod_name": "gpu-dev-abc12345",
        "ssh_command": "ssh dev@host",
        "gpu_type": "h100",
    }
    base.update(overrides)
    return base


def test_get_connection_info_single_match():
    mgr = _make_mgr()
    res = _single_reservation()
    mgr._table.query.return_value = {"Items": [res]}
    info = mgr.get_connection_info("abc12345", "octocat")

    assert info is not None
    assert info["reservation_id"] == res["reservation_id"]
    assert info["pod_name"] == "gpu-dev-abc12345"
    assert info["ssh_command"] == "ssh dev@host"
    assert info["status"] == "active"
    assert info["gpu_count"] == Decimal("2")
    assert info["is_multinode"] is False
    # query was issued against UserIndex with the user_id key
    _, kwargs = mgr._table.query.call_args
    assert kwargs["IndexName"] == "UserIndex"
    assert kwargs["ExpressionAttributeValues"][":user_id"] == "octocat"
    assert kwargs["ExpressionAttributeValues"][":rid"] == "abc12345"


def test_get_connection_info_no_match_falls_back_to_scan_then_none():
    mgr = _make_mgr()
    mgr._table.query.return_value = {"Items": []}
    mgr._table.scan.return_value = {"Items": []}
    info = mgr.get_connection_info("nomatch", "octocat")
    assert info is None
    mgr._table.scan.assert_called_once()


def test_get_connection_info_scan_fallback_finds_added_user_reservation():
    """A user who is not the owner (UserIndex empty) is served by the scan
    fallback that matches the reservation_id prefix."""
    mgr = _make_mgr()
    res = _single_reservation(user_id="someone-else")
    mgr._table.query.return_value = {"Items": []}
    mgr._table.scan.return_value = {"Items": [res]}
    info = mgr.get_connection_info("abc12345", "octocat")
    assert info is not None
    assert info["reservation_id"] == res["reservation_id"]


def test_get_connection_info_ambiguous_prefix_returns_none():
    mgr = _make_mgr()
    r1 = _single_reservation(reservation_id="abc11111-x")
    r2 = _single_reservation(reservation_id="abc22222-y")
    # Both begin with "abc" -> ambiguous
    mgr._table.query.return_value = {"Items": [r1, r2]}
    info = mgr.get_connection_info("abc", "octocat")
    assert info is None


def test_get_connection_info_defaults_for_missing_fields():
    mgr = _make_mgr()
    res = {
        "reservation_id": "deadbeef-0000",
        "user_id": "octocat",
        "gpu_count": 1,
        "status": "pending",
    }
    mgr._table.query.return_value = {"Items": [res]}
    info = mgr.get_connection_info("deadbeef", "octocat")
    assert info["ssh_command"] == "ssh user@pending"
    assert info["pod_name"] == "pending"
    assert info["namespace"] == "default"
    assert info["instance_type"] == "unknown"
    assert info["gpu_type"] == "unknown"
    assert info["secondary_users"] == []


def test_get_connection_info_multinode_assembles_nodes():
    mgr = _make_mgr()
    master = "master-xyz"
    n0 = _single_reservation(
        reservation_id="node0-aaaa", is_multinode=True,
        master_reservation_id=master, node_index=0, pod_name="pod-0",
    )
    n1 = _single_reservation(
        reservation_id="node1-bbbb", is_multinode=True,
        master_reservation_id=master, node_index=1, pod_name="pod-1",
    )
    # Query returns both; lookup is for node0's prefix specifically.
    mgr._table.query.return_value = {"Items": [n1, n0]}
    info = mgr.get_connection_info("node0", "octocat")

    assert info is not None
    assert info["is_multinode"] is True
    assert info["total_nodes"] == 2
    # nodes sorted by node_index ascending
    assert [n["node_index"] for n in info["nodes"]] == [0, 1]
    assert info["nodes"][0]["pod_name"] == "pod-0"
    assert info["nodes"][1]["pod_name"] == "pod-1"


def test_get_connection_info_paginates_query():
    mgr = _make_mgr()
    res = _single_reservation()
    page1 = {"Items": [], "LastEvaluatedKey": {"k": "v"}}
    page2 = {"Items": [res]}
    mgr._table.query.side_effect = [page1, page2]
    info = mgr.get_connection_info("abc12345", "octocat")
    assert info is not None
    assert mgr._table.query.call_count == 2


def test_get_connection_info_exception_returns_none():
    mgr = _make_mgr()
    mgr._table.query.side_effect = RuntimeError("ddb down")
    assert mgr.get_connection_info("abc", "octocat") is None


# --------------------------------------------------------------------------- #
# get_gpu_availability_by_type                                                 #
# --------------------------------------------------------------------------- #
def test_availability_coerces_decimals_and_passes_size_etas():
    mgr = _make_mgr()
    avail_table = MagicMock(name="availability_table")
    avail_table.scan.return_value = {
        "Items": [
            {
                "gpu_type": "h100",
                "available_gpus": Decimal("4"),
                "total_gpus": Decimal("8"),
                "max_reservable": Decimal("8"),
                "full_nodes_available": Decimal("0"),
                "gpus_per_instance": Decimal("8"),
                "running_instances": Decimal("2"),
                "desired_capacity": Decimal("2"),
                "size_etas": {"4": Decimal("1717000000"), "8": Decimal("1717100000")},
                "maintenance": False,
            }
        ]
    }
    mgr._cfg.dynamodb.Table.return_value = avail_table
    with patch.object(mgr, "_get_queue_length_for_gpu_type", return_value=3):
        out = mgr.get_gpu_availability_by_type()

    h = out["h100"]
    assert h["available"] == 4 and isinstance(h["available"], int)
    assert h["total"] == 8
    assert h["max_reservable"] == 8
    assert h["gpus_per_instance"] == 8
    assert h["running_instances"] == 2
    assert h["queue_length"] == 3
    # estimated wait = queue_length * 15
    assert h["estimated_wait_minutes"] == 45
    # size_etas keys -> str, values -> int
    assert h["size_etas"] == {"4": 1717000000, "8": 1717100000}
    assert all(isinstance(v, int) for v in h["size_etas"].values())
    assert h["maintenance"] is False


def test_availability_zero_queue_zero_wait():
    mgr = _make_mgr()
    avail_table = MagicMock()
    avail_table.scan.return_value = {
        "Items": [{"gpu_type": "t4", "available_gpus": Decimal("8"),
                   "total_gpus": Decimal("8")}]
    }
    mgr._cfg.dynamodb.Table.return_value = avail_table
    with patch.object(mgr, "_get_queue_length_for_gpu_type", return_value=0):
        out = mgr.get_gpu_availability_by_type()
    assert out["t4"]["queue_length"] == 0
    assert out["t4"]["estimated_wait_minutes"] == 0


def test_availability_paginates_scan():
    mgr = _make_mgr()
    avail_table = MagicMock()
    avail_table.scan.side_effect = [
        {"Items": [{"gpu_type": "h100", "available_gpus": Decimal("1"),
                    "total_gpus": Decimal("8")}],
         "LastEvaluatedKey": {"gpu_type": "h100"}},
        {"Items": [{"gpu_type": "b200", "available_gpus": Decimal("8"),
                    "total_gpus": Decimal("8")}]},
    ]
    mgr._cfg.dynamodb.Table.return_value = avail_table
    with patch.object(mgr, "_get_queue_length_for_gpu_type", return_value=0):
        out = mgr.get_gpu_availability_by_type()
    assert set(out) == {"h100", "b200"}
    assert avail_table.scan.call_count == 2


def test_availability_missing_numeric_fields_default_to_zero():
    mgr = _make_mgr()
    avail_table = MagicMock()
    avail_table.scan.return_value = {"Items": [{"gpu_type": "cpu-spot"}]}
    mgr._cfg.dynamodb.Table.return_value = avail_table
    with patch.object(mgr, "_get_queue_length_for_gpu_type", return_value=0):
        out = mgr.get_gpu_availability_by_type()
    c = out["cpu-spot"]
    assert c["available"] == 0
    assert c["total"] == 0
    assert c["size_etas"] == {}
    assert c["spot_info"] == {}


def test_availability_returns_none_on_scan_error():
    mgr = _make_mgr()
    avail_table = MagicMock()
    avail_table.scan.side_effect = RuntimeError("scan boom")
    mgr._cfg.dynamodb.Table.return_value = avail_table
    assert mgr.get_gpu_availability_by_type() is None


# --------------------------------------------------------------------------- #
# get_version                                                                  #
# --------------------------------------------------------------------------- #
def test_get_version_is_str():
    assert isinstance(get_version(), str)
