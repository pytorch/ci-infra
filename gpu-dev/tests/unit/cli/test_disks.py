"""Unit tests for gpu_dev_cli.disks — disk listing / in-use status / create /
delete / clone / rename / unlock / poll helpers.

These are pure-logic + boto3-shaped tests. We never touch real AWS: the Config
is a MagicMock whose ``session``/``dynamodb`` produce MagicMock clients and
resources, and the module's own client accessors
(``get_dynamodb_resource``/``get_ec2_client``/``get_s3_client``) are patched
where it matters. ``list_disks`` is the gatekeeper for the mutation helpers, so
most of those tests patch ``disks.list_disks`` directly and assert the branch /
SQS-message behaviour.

The ``__no_disk__`` sentinel itself lives in interactive.select_disk_interactive;
here we cover the disks.py side that consumes/produces disk records and the
formatting/coercion done in list_disks.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

import gpu_dev_cli.disks as disks


# --------------------------------------------------------------------------- #
# helpers / fixtures                                                           #
# --------------------------------------------------------------------------- #
def make_config(**overrides):
    """A Config-shaped MagicMock with the attribute names disks.py reads."""
    cfg = MagicMock(name="config")
    cfg.aws_region = "us-east-2"
    cfg.disks_table = "pytorch-gpu-dev-disks"
    cfg.reservations_table = "pytorch-gpu-dev-reservations"
    cfg.operations_table = "pytorch-gpu-dev-operations"
    cfg.queue_name = "pytorch-gpu-dev-reservation-queue"
    cfg.get_queue_url.return_value = "https://sqs/queue"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class _Table:
    """Minimal DynamoDB Table double driven by canned responses."""

    def __init__(self, query_responses=None, get_item_response=None):
        # query_responses: list of dicts returned in sequence by query()
        self._query_responses = list(query_responses or [{"Items": []}])
        self._get_item_response = get_item_response or {}
        self.query_calls = []
        self.get_item_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self._query_responses:
            return self._query_responses.pop(0)
        return {"Items": []}

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        return self._get_item_response


def fake_dynamodb(tables):
    """A dynamodb resource double; tables is {name: _Table}."""
    res = MagicMock(name="dynamodb_resource")
    res.Table.side_effect = lambda name: tables[name]
    return res


# --------------------------------------------------------------------------- #
# client accessor helpers                                                      #
# --------------------------------------------------------------------------- #
def test_get_ec2_client_uses_region_and_session():
    cfg = make_config()
    client = disks.get_ec2_client(cfg)
    cfg.session.client.assert_called_once_with("ec2", region_name="us-east-2")
    assert client is cfg.session.client.return_value


def test_get_s3_client_uses_region_and_session():
    cfg = make_config()
    disks.get_s3_client(cfg)
    cfg.session.client.assert_called_once_with("s3", region_name="us-east-2")


def test_get_dynamodb_resource_uses_region_and_session():
    cfg = make_config()
    disks.get_dynamodb_resource(cfg)
    cfg.session.resource.assert_called_once_with("dynamodb", region_name="us-east-2")


# --------------------------------------------------------------------------- #
# list_disks: coercion / formatting / filtering / sort                         #
# --------------------------------------------------------------------------- #
def _list_disks_with(disk_items, reservation_active_items=None):
    """Run list_disks with a disks-table query returning disk_items and the
    reservations UserStatusIndex returning reservation_active_items (per status).
    """
    cfg = make_config()
    disks_table = _Table(query_responses=[{"Items": disk_items}])
    # reservations table is queried once per status (4 statuses); give the same
    # batch on the first call and empty afterwards.
    res_responses = [{"Items": reservation_active_items or []}] + [{"Items": []}] * 4
    reservations_table = _Table(query_responses=res_responses)
    cfg.dynamodb = fake_dynamodb({
        cfg.disks_table: disks_table,
        cfg.reservations_table: reservations_table,
    })
    return disks.list_disks("octocat", cfg), disks_table, reservations_table


def test_list_disks_coerces_decimal_sizes_to_int():
    item = {
        "disk_name": "default",
        "size_gb": Decimal("200"),
        "snapshot_count": Decimal("3"),
        "pending_snapshot_count": Decimal("1"),
    }
    result, _, _ = _list_disks_with([item])
    assert len(result) == 1
    d = result[0]
    assert d["name"] == "default"
    assert d["size_gb"] == 200 and isinstance(d["size_gb"], int)
    assert d["snapshot_count"] == 3 and isinstance(d["snapshot_count"], int)
    assert d["pending_snapshot_count"] == 1 and isinstance(d["pending_snapshot_count"], int)


def test_list_disks_missing_numeric_fields_default_to_zero():
    result, _, _ = _list_disks_with([{"disk_name": "fresh"}])
    d = result[0]
    assert d["size_gb"] == 0
    assert d["snapshot_count"] == 0
    assert d["pending_snapshot_count"] == 0
    # absent attached_to_reservation -> None (empty string -> None)
    assert d["reservation_id"] is None
    assert d["in_use"] is False


def test_list_disks_parses_and_normalizes_naive_datetimes_to_utc():
    item = {
        "disk_name": "d1",
        # naive ISO strings -> must be tagged utc
        "created_at": "2026-01-01T00:00:00",
        "last_used": "2026-01-02T12:00:00",
    }
    result, _, _ = _list_disks_with([item])
    d = result[0]
    assert d["created_at"].tzinfo is timezone.utc
    assert d["last_used"].tzinfo is timezone.utc
    assert d["created_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_list_disks_preserves_aware_datetime_offset():
    item = {"disk_name": "d1", "created_at": "2026-01-01T00:00:00+00:00"}
    result, _, _ = _list_disks_with([item])
    assert result[0]["created_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_list_disks_none_datetimes_when_missing():
    result, _, _ = _list_disks_with([{"disk_name": "d1"}])
    assert result[0]["created_at"] is None
    assert result[0]["last_used"] is None


def test_list_disks_in_use_from_disks_table_field():
    item = {
        "disk_name": "d1",
        "in_use": True,
        "attached_to_reservation": "res-12345678",
    }
    result, _, _ = _list_disks_with([item])
    d = result[0]
    assert d["in_use"] is True
    assert d["reservation_id"] == "res-12345678"


def test_list_disks_active_reservation_overrides_and_truncates_rid():
    """A matching active reservation flips in_use True and uses rid[:8]."""
    disk_items = [{"disk_name": "myproj", "in_use": False}]
    active = [{"reservation_id": "abcdefgh-1111-2222-3333", "disk_name": "myproj"}]
    result, _, res_table = _list_disks_with(disk_items, reservation_active_items=active)
    d = result[0]
    assert d["in_use"] is True
    assert d["reservation_id"] == "abcdefgh"  # truncated to 8
    # queried the reservations index once per status (active/preparing/queued/pending)
    assert len(res_table.query_calls) == 4


def test_list_disks_reservation_without_disk_name_is_ignored():
    disk_items = [{"disk_name": "myproj", "in_use": False}]
    active = [{"reservation_id": "abcd1234", "disk_name": None}]
    result, _, _ = _list_disks_with(disk_items, reservation_active_items=active)
    assert result[0]["in_use"] is False


def test_list_disks_reservation_batch_check_swallows_errors():
    """If the reservations batch query raises, list still returns disk records."""
    cfg = make_config()
    disks_table = _Table(query_responses=[{"Items": [{"disk_name": "d1"}]}])
    bad_res = MagicMock()
    bad_res.query.side_effect = RuntimeError("ddb down")
    cfg.dynamodb = fake_dynamodb({
        cfg.disks_table: disks_table,
        cfg.reservations_table: bad_res,
    })
    result = disks.list_disks("octocat", cfg)
    assert len(result) == 1 and result[0]["name"] == "d1"


def test_list_disks_filters_expired_soft_deleted():
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    items = [
        {"disk_name": "expired", "is_deleted": True, "delete_date": yesterday},
        {"disk_name": "pending-del", "is_deleted": True, "delete_date": tomorrow},
        {"disk_name": "alive"},
    ]
    result, _, _ = _list_disks_with(items)
    names = {d["name"] for d in result}
    # expired soft-delete removed; future-dated soft-delete & alive kept
    assert names == {"pending-del", "alive"}


def test_list_disks_sorts_by_last_used_desc():
    items = [
        {"disk_name": "old", "last_used": "2025-01-01T00:00:00+00:00"},
        {"disk_name": "new", "last_used": "2026-05-01T00:00:00+00:00"},
        {"disk_name": "never"},  # no last_used -> sorts last
    ]
    result, _, _ = _list_disks_with(items)
    assert [d["name"] for d in result] == ["new", "old", "never"]


def test_list_disks_paginates_disks_table():
    cfg = make_config()
    page1 = {"Items": [{"disk_name": "a"}], "LastEvaluatedKey": {"k": 1}}
    page2 = {"Items": [{"disk_name": "b"}]}
    disks_table = _Table(query_responses=[page1, page2])
    reservations_table = _Table(query_responses=[{"Items": []}] * 4)
    cfg.dynamodb = fake_dynamodb({
        cfg.disks_table: disks_table,
        cfg.reservations_table: reservations_table,
    })
    result = disks.list_disks("octocat", cfg)
    assert {d["name"] for d in result} == {"a", "b"}
    # second query passed ExclusiveStartKey
    assert disks_table.query_calls[1].get("ExclusiveStartKey") == {"k": 1}


# --------------------------------------------------------------------------- #
# get_disk_in_use_status                                                       #
# --------------------------------------------------------------------------- #
def _patch_ddb(monkeypatch, tables):
    cfg = make_config()
    res = fake_dynamodb(tables)
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    return cfg


def test_in_use_status_true_from_disks_table(monkeypatch):
    disks_table = _Table(get_item_response={
        "Item": {"in_use": True, "attached_to_reservation": "res-abc"}
    })
    cfg = make_config()
    cfg.dynamodb = None
    res = fake_dynamodb({cfg.disks_table: disks_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    in_use, rid = disks.get_disk_in_use_status("default", "octocat", cfg)
    assert in_use is True
    assert rid == "res-abc"
    # short-circuits before touching reservations table
    assert disks_table.get_item_calls[0]["Key"] == {"user_id": "octocat", "disk_name": "default"}


def test_in_use_status_falls_through_to_reservations(monkeypatch):
    disks_table = _Table(get_item_response={"Item": {}})  # not in_use
    res_table = _Table(query_responses=[{"Items": [{"reservation_id": "r-99"}]}])
    cfg = make_config()
    res = fake_dynamodb({cfg.disks_table: disks_table, cfg.reservations_table: res_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    in_use, rid = disks.get_disk_in_use_status("myproj", "octocat", cfg)
    assert in_use is True
    assert rid == "r-99"


def test_in_use_status_default_disk_legacy_match(monkeypatch):
    """For 'default', a legacy reservation (no disk_name, has ebs vol) counts."""
    disks_table = _Table(get_item_response={"Item": {}})
    # first reservations query (named disk) returns nothing; second (legacy) hits
    res_table = _Table(query_responses=[
        {"Items": []},
        {"Items": [{"reservation_id": "legacy-1"}]},
    ])
    cfg = make_config()
    res = fake_dynamodb({cfg.disks_table: disks_table, cfg.reservations_table: res_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    in_use, rid = disks.get_disk_in_use_status("default", "octocat", cfg)
    assert in_use is True
    assert rid == "legacy-1"


def test_in_use_status_non_default_skips_legacy_query(monkeypatch):
    disks_table = _Table(get_item_response={"Item": {}})
    res_table = _Table(query_responses=[{"Items": []}])
    cfg = make_config()
    res = fake_dynamodb({cfg.disks_table: disks_table, cfg.reservations_table: res_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    in_use, rid = disks.get_disk_in_use_status("myproj", "octocat", cfg)
    assert (in_use, rid) == (False, None)
    # only ONE reservations query (named); the legacy branch is default-only
    assert len(res_table.query_calls) == 1


def test_in_use_status_outer_exception_returns_false(monkeypatch, capsys):
    """A failure in the reservations query (inside the outer try) returns
    (False, None) + warns. Note: the disks-table get_item has its OWN inner
    try/except, so to reach the outer handler the failure must come from the
    reservations query."""
    disks_table = _Table(get_item_response={"Item": {}})  # not in_use, ok
    bad_res = MagicMock()
    bad_res.query.side_effect = RuntimeError("creds expired")
    cfg = make_config()
    res = fake_dynamodb({cfg.disks_table: disks_table, cfg.reservations_table: bad_res})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    in_use, rid = disks.get_disk_in_use_status("default", "octocat", cfg)
    assert (in_use, rid) == (False, None)
    assert "Could not query reservations" in capsys.readouterr().out


def test_in_use_status_disks_table_error_falls_through(monkeypatch):
    """A failure reading the disks table doesn't abort — reservations are checked."""
    bad_disks = MagicMock()
    bad_disks.get_item.side_effect = RuntimeError("throttled")
    res_table = _Table(query_responses=[{"Items": [{"reservation_id": "r-7"}]}])
    cfg = make_config()
    res = fake_dynamodb({cfg.disks_table: bad_disks, cfg.reservations_table: res_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    in_use, rid = disks.get_disk_in_use_status("myproj", "octocat", cfg)
    assert (in_use, rid) == (True, "r-7")


# --------------------------------------------------------------------------- #
# create_disk                                                                  #
# --------------------------------------------------------------------------- #
def test_create_disk_rejects_existing(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "dup"}])
    cfg = make_config()
    assert disks.create_disk("dup", "octocat", cfg) is None
    assert "already exists" in capsys.readouterr().out
    cfg.session.client.assert_not_called()


@pytest.mark.parametrize("bad", ["has space", "weird!", "tab\tname", "slash/name", ""])
def test_create_disk_rejects_invalid_names(monkeypatch, capsys, bad):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    cfg = make_config()
    assert disks.create_disk(bad, "octocat", cfg) is None
    assert "only letters, numbers" in capsys.readouterr().out


def test_create_disk_sends_sqs_and_returns_operation_id(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    monkeypatch.setattr(disks, "get_version", lambda: "0.6.6")
    cfg = make_config()
    sqs = MagicMock()
    cfg.session.client.return_value = sqs

    op_id = disks.create_disk("valid_name-1", "octocat", cfg)

    assert op_id and isinstance(op_id, str)
    cfg.session.client.assert_called_once_with("sqs", region_name="us-east-2")
    sqs.send_message.assert_called_once()
    kwargs = sqs.send_message.call_args.kwargs
    assert kwargs["QueueUrl"] == "https://sqs/queue"
    import json
    body = json.loads(kwargs["MessageBody"])
    assert body["action"] == "create_disk"
    assert body["disk_name"] == "valid_name-1"
    assert body["user_id"] == "octocat"
    assert body["operation_id"] == op_id
    assert body["version"] == "0.6.6"


def test_create_disk_sqs_error_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    monkeypatch.setattr(disks, "get_version", lambda: "0")
    cfg = make_config()
    sqs = MagicMock()
    sqs.send_message.side_effect = RuntimeError("no perms")
    cfg.session.client.return_value = sqs
    assert disks.create_disk("ok", "octocat", cfg) is None
    assert "Error sending create request" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# delete_disk                                                                  #
# --------------------------------------------------------------------------- #
def test_delete_disk_not_found(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    cfg = make_config()
    assert disks.delete_disk("ghost", "octocat", cfg) is None
    assert "not found" in capsys.readouterr().out


def test_delete_disk_in_use_blocked(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "busy", "in_use": True, "reservation_id": "r-1"}])
    cfg = make_config()
    assert disks.delete_disk("busy", "octocat", cfg) is None
    out = capsys.readouterr().out
    assert "currently in use" in out
    assert "r-1" in out
    cfg.session.client.assert_not_called()


def test_delete_disk_sends_sqs_with_delete_date(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "old", "in_use": False, "reservation_id": None}])
    monkeypatch.setattr(disks, "get_version", lambda: "1.0")
    cfg = make_config()
    sqs = MagicMock()
    cfg.session.client.return_value = sqs

    op_id = disks.delete_disk("old", "octocat", cfg)
    assert op_id
    import json
    body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert body["action"] == "delete_disk"
    assert body["disk_name"] == "old"
    # delete_date is 30 days out, YYYY-MM-DD
    expected = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    assert body["delete_date"] == expected


def test_delete_disk_sqs_error_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "old", "in_use": False}])
    monkeypatch.setattr(disks, "get_version", lambda: "1.0")
    cfg = make_config()
    sqs = MagicMock()
    sqs.send_message.side_effect = RuntimeError("boom")
    cfg.session.client.return_value = sqs
    assert disks.delete_disk("old", "octocat", cfg) is None
    assert "Error sending delete request" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# clone_disk                                                                   #
# --------------------------------------------------------------------------- #
def _src(**kw):
    base = {"name": "src", "is_deleted": False, "snapshot_count": 2}
    base.update(kw)
    return base


def test_clone_disk_source_missing(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    assert disks.clone_disk("src", "dst", "octocat", make_config()) is None
    assert "Source disk 'src' not found" in capsys.readouterr().out


def test_clone_disk_source_deleted(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [_src(is_deleted=True)])
    assert disks.clone_disk("src", "dst", "octocat", make_config()) is None
    assert "marked for deletion" in capsys.readouterr().out


def test_clone_disk_source_no_snapshots(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [_src(snapshot_count=0)])
    assert disks.clone_disk("src", "dst", "octocat", make_config()) is None
    assert "no snapshots to clone" in capsys.readouterr().out


def test_clone_disk_target_exists(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [_src(), {"name": "dst"}])
    assert disks.clone_disk("src", "dst", "octocat", make_config()) is None
    assert "Disk 'dst' already exists" in capsys.readouterr().out


def test_clone_disk_invalid_target_name(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [_src()])
    assert disks.clone_disk("src", "bad name", "octocat", make_config()) is None
    assert "only letters, numbers" in capsys.readouterr().out


def test_clone_disk_success_sends_sqs(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [_src()])
    monkeypatch.setattr(disks, "get_version", lambda: "2.0")
    cfg = make_config()
    sqs = MagicMock()
    cfg.session.client.return_value = sqs
    op_id = disks.clone_disk("src", "dst-1", "octocat", cfg)
    assert op_id
    import json
    body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert body["action"] == "clone_disk"
    assert body["source_disk"] == "src"
    assert body["target_disk"] == "dst-1"


def test_clone_disk_sqs_error_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [_src()])
    monkeypatch.setattr(disks, "get_version", lambda: "2.0")
    cfg = make_config()
    sqs = MagicMock()
    sqs.send_message.side_effect = RuntimeError("x")
    cfg.session.client.return_value = sqs
    assert disks.clone_disk("src", "dst", "octocat", cfg) is None
    assert "Error sending clone request" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# unlock_disk                                                                  #
# --------------------------------------------------------------------------- #
def test_unlock_disk_not_found(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    assert disks.unlock_disk("x", "octocat", make_config()) is False
    assert "not found" in capsys.readouterr().out


def test_unlock_disk_locked_sends_clear_lock(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "x", "in_use": True}])
    monkeypatch.setattr(disks, "get_version", lambda: "1")
    cfg = make_config()
    sqs = MagicMock()
    cfg.session.client.return_value = sqs
    assert disks.unlock_disk("x", "octocat", cfg) is True
    import json
    body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert body["action"] == "clear_disk_lock"
    assert body["disk_name"] == "x"


def test_unlock_disk_not_locked_no_ebs_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "x", "in_use": False}])
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_volumes.return_value = {"Volumes": []}
    cfg.session.client.return_value = ec2
    assert disks.unlock_disk("x", "octocat", cfg) is False
    assert "is not locked" in capsys.readouterr().out


def test_unlock_disk_not_locked_but_ebs_attached_force_detaches(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "x", "in_use": False}])
    monkeypatch.setattr(disks, "get_version", lambda: "1")
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_volumes.return_value = {"Volumes": [{"VolumeId": "vol-1"}]}
    sqs = MagicMock()
    # session.client called for ec2 (describe) then sqs (send_message)
    cfg.session.client.side_effect = [ec2, sqs]
    assert disks.unlock_disk("x", "octocat", cfg) is True
    assert "still attached" in capsys.readouterr().out
    sqs.send_message.assert_called_once()


def test_unlock_disk_describe_volumes_error_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "x", "in_use": False}])
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_volumes.side_effect = RuntimeError("denied")
    cfg.session.client.return_value = ec2
    assert disks.unlock_disk("x", "octocat", cfg) is False
    assert "is not locked" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# rename_disk                                                                  #
# --------------------------------------------------------------------------- #
def test_rename_disk_invalid_new_name(monkeypatch, capsys):
    cfg = make_config()
    cfg.session.client.return_value = MagicMock()
    assert disks.rename_disk("old", "new name", "octocat", cfg) is False
    assert "only letters, numbers" in capsys.readouterr().out


def test_rename_disk_old_missing(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    cfg = make_config()
    cfg.session.client.return_value = MagicMock()
    assert disks.rename_disk("old", "new", "octocat", cfg) is False
    assert "Disk 'old' not found" in capsys.readouterr().out


def test_rename_disk_new_exists(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "old", "in_use": False}, {"name": "new", "in_use": False}])
    cfg = make_config()
    cfg.session.client.return_value = MagicMock()
    assert disks.rename_disk("old", "new", "octocat", cfg) is False
    assert "Disk 'new' already exists" in capsys.readouterr().out


def test_rename_disk_in_use_blocked(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "old", "in_use": True, "reservation_id": "r-9"}])
    cfg = make_config()
    cfg.session.client.return_value = MagicMock()
    assert disks.rename_disk("old", "new", "octocat", cfg) is False
    out = capsys.readouterr().out
    assert "currently in use" in out
    assert "r-9" in out


def test_rename_disk_no_snapshots_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "old", "in_use": False}])
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_snapshots.return_value = {"Snapshots": []}
    cfg.session.client.return_value = ec2
    assert disks.rename_disk("old", "new", "octocat", cfg) is False
    assert "No snapshots found" in capsys.readouterr().out


def test_rename_disk_retags_snapshots(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "old", "in_use": False}])
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_snapshots.return_value = {"Snapshots": [
        {"SnapshotId": "snap-1"}, {"SnapshotId": "snap-2"}]}
    cfg.session.client.return_value = ec2
    assert disks.rename_disk("old", "newname", "octocat", cfg) is True
    assert ec2.create_tags.call_count == 2
    # tags set disk_name -> new
    first = ec2.create_tags.call_args_list[0].kwargs
    assert first["Resources"] == ["snap-1"]
    assert {"Key": "disk_name", "Value": "newname"} in first["Tags"]
    out = capsys.readouterr().out
    assert "2 snapshots updated" in out


def test_rename_disk_partial_tag_failure_still_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "old", "in_use": False}])
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_snapshots.return_value = {"Snapshots": [
        {"SnapshotId": "snap-1"}, {"SnapshotId": "snap-2"}]}
    ec2.create_tags.side_effect = [None, RuntimeError("tag fail")]
    cfg.session.client.return_value = ec2
    assert disks.rename_disk("old", "newname", "octocat", cfg) is True
    out = capsys.readouterr().out
    # one updated, one errored, count reflects only successes
    assert "1 snapshots updated" in out
    assert "Error updating snapshot snap-2" in out


def test_rename_disk_describe_snapshots_error_returns_false(monkeypatch, capsys):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "old", "in_use": False}])
    cfg = make_config()
    ec2 = MagicMock()
    ec2.describe_snapshots.side_effect = RuntimeError("api down")
    cfg.session.client.return_value = ec2
    assert disks.rename_disk("old", "new", "octocat", cfg) is False
    assert "Error renaming disk" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# poll_disk_operation                                                          #
# --------------------------------------------------------------------------- #
def test_poll_create_returns_on_appearance(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [{"name": "new"}])
    ok, msg = disks.poll_disk_operation("create", "new", "octocat", make_config())
    assert ok is True
    assert "created successfully" in msg


def test_poll_create_timeout(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    # speed up loop and force immediate timeout
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok, msg = disks.poll_disk_operation("create", "new", "octocat", make_config(),
                                        timeout_seconds=0)
    assert ok is False
    assert "Timed out" in msg


def test_poll_delete_marks_deleted(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "d", "is_deleted": True, "delete_date": "2026-07-01"}])
    ok, msg = disks.poll_disk_operation("delete", "d", "octocat", make_config())
    assert ok is True
    assert "marked for deletion" in msg
    assert "2026-07-01" in msg


def test_poll_delete_disk_gone(monkeypatch):
    monkeypatch.setattr(disks, "list_disks", lambda u, c: [])
    ok, msg = disks.poll_disk_operation("delete", "d", "octocat", make_config())
    assert ok is True
    assert "deleted successfully" in msg


def test_poll_delete_timeout(monkeypatch):
    # disk still present and not deleted -> loops to timeout
    monkeypatch.setattr(disks, "list_disks", lambda u, c:
                        [{"name": "d", "is_deleted": False}])
    monkeypatch.setattr("time.sleep", lambda s: None)
    ok, msg = disks.poll_disk_operation("delete", "d", "octocat", make_config(),
                                        timeout_seconds=0)
    assert ok is False
    assert "Timed out" in msg


# --------------------------------------------------------------------------- #
# poll_operation (operations table)                                           #
# --------------------------------------------------------------------------- #
def test_poll_operation_completed(monkeypatch):
    ops_table = _Table(get_item_response={"Item": {"status": "completed", "error": None}})
    cfg = make_config()
    res = fake_dynamodb({cfg.operations_table: ops_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    status, err = disks.poll_operation("op-1", cfg)
    assert status == "completed"
    assert err is None


def test_poll_operation_failed_with_error(monkeypatch):
    ops_table = _Table(get_item_response={"Item": {"status": "failed", "error": "nope"}})
    cfg = make_config()
    res = fake_dynamodb({cfg.operations_table: ops_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    status, err = disks.poll_operation("op-1", cfg)
    assert (status, err) == ("failed", "nope")


def test_poll_operation_get_item_error_returns_failed(monkeypatch, capsys):
    ops_table = MagicMock()
    ops_table.get_item.side_effect = RuntimeError("ddb error")
    cfg = make_config()
    res = MagicMock()
    res.Table.return_value = ops_table
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    status, err = disks.poll_operation("op-1", cfg)
    assert status == "failed"
    assert "ddb error" in err
    assert "Error polling operation" in capsys.readouterr().out


def test_poll_operation_timeout(monkeypatch):
    ops_table = _Table(get_item_response={})  # no Item
    cfg = make_config()
    res = fake_dynamodb({cfg.operations_table: ops_table})
    monkeypatch.setattr(disks, "get_dynamodb_resource", lambda c: res)
    monkeypatch.setattr("time.sleep", lambda s: None)
    status, err = disks.poll_operation("op-1", cfg, timeout_seconds=0)
    assert (status, err) == (None, None)
