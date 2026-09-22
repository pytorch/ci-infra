"""Unit tests for reservation_processor lambda cancellation logic.

Targets:
    index.find_reservation_by_prefix  -- resolve full UUID / prefix scoped to user
    index.process_cancellation_request -- authorize + status-gate + cancel

All AWS access goes through the ``aws_mocks`` fixture (dynamodb is a MagicMock
whose ``.Table(...)`` returns a stable child mock). No network / no real boto3.
"""
import json

import pytest


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _table(aws_mocks):
    """The single table mock returned by ``dynamodb.Table(...)``."""
    return aws_mocks["dynamodb"].Table.return_value


def _full_uuid(seed="a"):
    # 36 chars, exactly 4 dashes -> matches the exact-match fast path
    return f"{seed * 8}-{seed * 4}-{seed * 4}-{seed * 4}-{seed * 12}"


def _sqs_record(body_dict):
    return {"body": json.dumps(body_dict)}


# --------------------------------------------------------------------------- #
# find_reservation_by_prefix -- exact (full UUID) path
# --------------------------------------------------------------------------- #
def test_full_uuid_exact_match_returns_item(lambda_index, aws_mocks):
    uuid = _full_uuid("a")
    item = {"reservation_id": uuid, "user_id": "alice", "status": "active"}
    _table(aws_mocks).get_item.return_value = {"Item": item}

    result = lambda_index.find_reservation_by_prefix(uuid, user_id="alice")

    assert result == item
    _table(aws_mocks).get_item.assert_called_once_with(Key={"reservation_id": uuid})
    # exact hit short-circuits; no prefix query/scan
    _table(aws_mocks).query.assert_not_called()
    _table(aws_mocks).scan.assert_not_called()


def test_full_uuid_exact_match_user_mismatch_raises(lambda_index, aws_mocks):
    uuid = _full_uuid("b")
    item = {"reservation_id": uuid, "user_id": "owner", "status": "active"}
    _table(aws_mocks).get_item.return_value = {"Item": item}
    # mismatched user: exact branch raises ValueError; it is caught and the code
    # falls through to a prefix query which also finds nothing -> "not found".
    _table(aws_mocks).query.return_value = {"Items": []}

    with pytest.raises(ValueError):
        lambda_index.find_reservation_by_prefix(uuid, user_id="intruder")


def test_full_uuid_no_item_falls_through_to_query(lambda_index, aws_mocks):
    uuid = _full_uuid("c")
    _table(aws_mocks).get_item.return_value = {}  # no "Item"
    matched = {"reservation_id": uuid, "user_id": "carol", "status": "queued"}
    _table(aws_mocks).query.return_value = {"Items": [matched]}

    result = lambda_index.find_reservation_by_prefix(uuid, user_id="carol")

    assert result == matched
    _table(aws_mocks).query.assert_called()  # fell through to prefix search


# --------------------------------------------------------------------------- #
# find_reservation_by_prefix -- prefix path with user_id (Query on UserIndex)
# --------------------------------------------------------------------------- #
def test_prefix_with_user_single_match(lambda_index, aws_mocks):
    item = {"reservation_id": "deadbeef-1111", "user_id": "dave", "status": "active"}
    _table(aws_mocks).query.return_value = {"Items": [item]}

    result = lambda_index.find_reservation_by_prefix("deadbeef", user_id="dave")

    assert result == item
    # user-scoped path uses query, not scan
    _table(aws_mocks).query.assert_called_once()
    _table(aws_mocks).scan.assert_not_called()
    # query is scoped to the UserIndex GSI
    _, kwargs = _table(aws_mocks).query.call_args
    assert kwargs["IndexName"] == "UserIndex"


def test_prefix_with_user_no_match_raises_not_found(lambda_index, aws_mocks):
    _table(aws_mocks).query.return_value = {"Items": []}

    with pytest.raises(ValueError, match="not found"):
        lambda_index.find_reservation_by_prefix("nomatch1", user_id="erin")


def test_prefix_with_user_ambiguous_raises(lambda_index, aws_mocks):
    items = [
        {"reservation_id": "abcd0001", "user_id": "frank"},
        {"reservation_id": "abcd0002", "user_id": "frank"},
    ]
    _table(aws_mocks).query.return_value = {"Items": items}

    with pytest.raises(ValueError, match="Ambiguous"):
        lambda_index.find_reservation_by_prefix("abcd", user_id="frank")


def test_prefix_with_user_paginates(lambda_index, aws_mocks):
    # First page has LastEvaluatedKey -> loop must run twice and aggregate.
    page1 = {"Items": [{"reservation_id": "p1", "user_id": "gail"}],
             "LastEvaluatedKey": {"reservation_id": "p1"}}
    page2 = {"Items": [{"reservation_id": "p2", "user_id": "gail"}]}
    _table(aws_mocks).query.side_effect = [page1, page2]

    # two matches -> ambiguous, but the point is both pages were consumed
    with pytest.raises(ValueError, match="Ambiguous"):
        lambda_index.find_reservation_by_prefix("p", user_id="gail")

    assert _table(aws_mocks).query.call_count == 2
    # second call carried the ExclusiveStartKey from page1
    _, second_kwargs = _table(aws_mocks).query.call_args_list[1]
    assert second_kwargs["ExclusiveStartKey"] == {"reservation_id": "p1"}


# --------------------------------------------------------------------------- #
# find_reservation_by_prefix -- prefix path WITHOUT user_id (Scan fallback)
# --------------------------------------------------------------------------- #
def test_prefix_without_user_uses_scan(lambda_index, aws_mocks):
    item = {"reservation_id": "cafe9999", "user_id": "anyone"}
    _table(aws_mocks).scan.return_value = {"Items": [item]}

    result = lambda_index.find_reservation_by_prefix("cafe9999")

    assert result == item
    _table(aws_mocks).scan.assert_called_once()
    _table(aws_mocks).query.assert_not_called()


def test_prefix_without_user_not_found_message_omits_user(lambda_index, aws_mocks):
    _table(aws_mocks).scan.return_value = {"Items": []}

    with pytest.raises(ValueError) as exc:
        lambda_index.find_reservation_by_prefix("ghostidx")

    # without user_id the message must NOT contain " for user "
    assert "for user" not in str(exc.value)


# --------------------------------------------------------------------------- #
# process_cancellation_request -- validation / not-found / auth
# --------------------------------------------------------------------------- #
def test_cancel_missing_user_id_returns_true_no_retry(lambda_index, aws_mocks):
    rec = _sqs_record({"reservation_id": "abcd1234"})  # no user_id
    assert lambda_index.process_cancellation_request(rec) is True
    # malformed -> never touches the table
    _table(aws_mocks).get_item.assert_not_called()
    _table(aws_mocks).query.assert_not_called()


def test_cancel_missing_reservation_id_returns_true(lambda_index, aws_mocks):
    rec = _sqs_record({"user_id": "alice"})  # no reservation_id
    assert lambda_index.process_cancellation_request(rec) is True


def test_cancel_malformed_json_body_returns_false(lambda_index, aws_mocks):
    rec = {"body": "not-json{{"}
    # JSON parse error -> outer except -> False (retry)
    assert lambda_index.process_cancellation_request(rec) is False


def test_cancel_reservation_not_found_returns_true(lambda_index, monkeypatch, aws_mocks):
    # find raises ValueError -> caught -> True (don't retry a vanished res)
    def _raise(rid, uid):
        raise ValueError("Reservation x not found for user alice")

    monkeypatch.setattr(lambda_index, "find_reservation_by_prefix", _raise)
    rec = _sqs_record({"reservation_id": "abcd1234", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is True


def test_cancel_db_error_during_find_returns_false(lambda_index, monkeypatch, aws_mocks):
    # non-ValueError from find -> caught as db_error -> False (retry)
    def _boom(rid, uid):
        raise RuntimeError("ddb throttled")

    monkeypatch.setattr(lambda_index, "find_reservation_by_prefix", _boom)
    rec = _sqs_record({"reservation_id": "abcd1234", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is False


@pytest.mark.parametrize("status", ["cancelled", "expired", "failed", "completed"])
def test_cancel_non_cancellable_status_returns_true(
    lambda_index, monkeypatch, aws_mocks, status
):
    reservation = {"reservation_id": _full_uuid("d"), "user_id": "alice", "status": status}
    monkeypatch.setattr(
        lambda_index, "find_reservation_by_prefix", lambda rid, uid: reservation
    )
    update = _spy(monkeypatch, lambda_index, "update_reservation_fields")

    rec = _sqs_record({"reservation_id": "dddddddd", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is True
    # gated out before any state mutation
    update.assert_not_called()


# --------------------------------------------------------------------------- #
# process_cancellation_request -- happy paths (queued / pending / preparing)
# --------------------------------------------------------------------------- #
def _spy(monkeypatch, mod, name):
    """Replace mod.name with a MagicMock and return it."""
    from unittest.mock import MagicMock

    m = MagicMock(name=name)
    monkeypatch.setattr(mod, name, m, raising=False)
    return m


@pytest.mark.parametrize("status", ["queued", "pending", "preparing"])
def test_cancel_non_active_marks_cancelled_returns_true(
    lambda_index, monkeypatch, aws_mocks, status
):
    full_id = _full_uuid("e")
    reservation = {
        "reservation_id": full_id,
        "user_id": "alice",
        "status": status,
        "disk_name": "default",
    }
    monkeypatch.setattr(
        lambda_index, "find_reservation_by_prefix", lambda rid, uid: reservation
    )
    update = _spy(monkeypatch, lambda_index, "update_reservation_fields")
    mark = _spy(monkeypatch, lambda_index, "mark_disk_in_use")

    rec = _sqs_record({"reservation_id": "eeeeeeee", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is True

    # status set to cancelled on the resolved FULL id (not the prefix)
    args, kwargs = update.call_args
    assert args[0] == full_id
    assert kwargs["status"] == "cancelled"
    assert "cancelled_at" in kwargs and "reservation_ended" in kwargs

    # non-active branch clears the disk in_use flag with in_use=False
    mark.assert_called_once_with("alice", "default", False)


def test_cancel_non_active_without_disk_skips_mark(lambda_index, monkeypatch, aws_mocks):
    reservation = {
        "reservation_id": _full_uuid("f"),
        "user_id": "alice",
        "status": "queued",
        # no disk_name
    }
    monkeypatch.setattr(
        lambda_index, "find_reservation_by_prefix", lambda rid, uid: reservation
    )
    _spy(monkeypatch, lambda_index, "update_reservation_fields")
    mark = _spy(monkeypatch, lambda_index, "mark_disk_in_use")

    rec = _sqs_record({"reservation_id": "ffffffff", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is True
    mark.assert_not_called()


def test_cancel_db_error_during_update_returns_false(lambda_index, monkeypatch, aws_mocks):
    reservation = {
        "reservation_id": _full_uuid("g"),
        "user_id": "alice",
        "status": "queued",
        "disk_name": "default",
    }
    monkeypatch.setattr(
        lambda_index, "find_reservation_by_prefix", lambda rid, uid: reservation
    )

    def _boom(*a, **k):
        raise RuntimeError("update failed")

    monkeypatch.setattr(lambda_index, "update_reservation_fields", _boom)

    rec = _sqs_record({"reservation_id": "gggggggg", "user_id": "alice"})
    # update raises inside inner try -> caught as db_error -> False (retry)
    assert lambda_index.process_cancellation_request(rec) is False


def test_cancel_active_creates_snapshot_and_cleans_up(lambda_index, monkeypatch, aws_mocks):
    full_id = _full_uuid("h")
    reservation = {
        "reservation_id": full_id,
        "user_id": "alice",
        "status": "active",
        "pod_name": "gpu-dev-alice-pod",
        "namespace": "gpu-dev",
        "disk_name": "default",
        "ebs_volume_id": "vol-1234",
    }
    monkeypatch.setattr(
        lambda_index, "find_reservation_by_prefix", lambda rid, uid: reservation
    )
    update = _spy(monkeypatch, lambda_index, "update_reservation_fields")
    cleanup = _spy(monkeypatch, lambda_index, "cleanup_pod_resources")
    mark = _spy(monkeypatch, lambda_index, "mark_disk_in_use")
    _spy(monkeypatch, lambda_index, "get_k8s_client")
    capture = _spy(monkeypatch, lambda_index, "capture_disk_contents")
    capture.return_value = ("s3://bucket/path", 1024)
    snap = _spy(monkeypatch, lambda_index, "safe_create_snapshot")
    snap.return_value = ("snap-9999", True)
    # ec2 describe_volumes used in force-detach branch
    aws_mocks_ec2 = _spy(monkeypatch, lambda_index, "ec2_client")
    aws_mocks_ec2.describe_volumes.return_value = {"Volumes": [{"State": "available"}]}

    rec = _sqs_record({"reservation_id": "hhhhhhhh", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is True

    update.assert_called_once()
    assert update.call_args.kwargs["status"] == "cancelled"
    snap.assert_called_once()
    assert snap.call_args.kwargs["volume_id"] == "vol-1234"
    cleanup.assert_called_once_with("gpu-dev-alice-pod", "gpu-dev")
    # active branch clears the disk flag exactly once (inside active handling)
    mark.assert_called_once_with("alice", "default", False)


def test_cancel_active_cleanup_failure_still_clears_disk_flag(
    lambda_index, monkeypatch, aws_mocks
):
    # cleanup_pod_resources raises, but disk in_use flag must still be cleared
    # (the flag clear lives OUTSIDE the cleanup try block).
    full_id = _full_uuid("i")
    reservation = {
        "reservation_id": full_id,
        "user_id": "alice",
        "status": "active",
        "pod_name": "pod-x",
        "namespace": "gpu-dev",
        "disk_name": "scratch",
        # no ebs_volume_id -> snapshot skipped
    }
    monkeypatch.setattr(
        lambda_index, "find_reservation_by_prefix", lambda rid, uid: reservation
    )
    _spy(monkeypatch, lambda_index, "update_reservation_fields")
    mark = _spy(monkeypatch, lambda_index, "mark_disk_in_use")

    def _cleanup_boom(*a, **k):
        raise RuntimeError("pod stuck")

    monkeypatch.setattr(lambda_index, "cleanup_pod_resources", _cleanup_boom)

    rec = _sqs_record({"reservation_id": "iiiiiiii", "user_id": "alice"})
    assert lambda_index.process_cancellation_request(rec) is True
    mark.assert_called_once_with("alice", "scratch", False)
