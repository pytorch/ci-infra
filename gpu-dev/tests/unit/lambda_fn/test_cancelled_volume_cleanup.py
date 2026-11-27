"""A cancelled reservation's detached volume is deleted once a completed snapshot
taken after the reservation ended backs it up.

The cancel path snapshots and force-detaches the volume but never deletes it, and
the expiry sweep skipped reservations whose pod was already gone, so every such
cancel leaked a 1 TiB volume.
"""
import importlib.util
import pathlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

import botocore.exceptions
import pytest

_EXPIRY = (
    pathlib.Path(__file__).resolve().parents[3]
    / "terraform-gpu-devservers" / "lambda" / "reservation_expiry" / "index.py"
)
ENDED = int(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def expiry():
    spec = importlib.util.spec_from_file_location("expiry_index", _EXPIRY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ec2(expiry, monkeypatch):
    ec2 = MagicMock()
    monkeypatch.setattr(expiry, "ec2_client", ec2)
    return ec2


def _setup(ec2, state="available", snap_hour=None):
    ec2.describe_volumes.return_value = {"Volumes": [{"VolumeId": "vol-1", "State": state}]}
    snaps = [] if snap_hour is None else [
        {"SnapshotId": "snap-1", "StartTime": datetime(2026, 10, 30, snap_hour, 0, tzinfo=timezone.utc)}]
    ec2.describe_snapshots.return_value = {"Snapshots": snaps}


def test_deletes_detached_volume_with_snapshot_after_end(expiry, ec2):
    _setup(ec2, snap_hour=13)
    assert expiry.delete_detached_volume_if_backed_up("vol-1", ENDED)
    ec2.delete_volume.assert_called_once_with(VolumeId="vol-1")


def test_keeps_volume_when_snapshot_predates_end(expiry, ec2):
    _setup(ec2, snap_hour=11)
    assert not expiry.delete_detached_volume_if_backed_up("vol-1", ENDED)
    ec2.delete_volume.assert_not_called()


def test_keeps_volume_without_completed_snapshot(expiry, ec2):
    _setup(ec2)
    assert not expiry.delete_detached_volume_if_backed_up("vol-1", ENDED)
    ec2.delete_volume.assert_not_called()


def test_keeps_attached_volume(expiry, ec2):
    _setup(ec2, state="in-use", snap_hour=13)
    assert not expiry.delete_detached_volume_if_backed_up("vol-1", ENDED)
    ec2.delete_volume.assert_not_called()


def test_already_deleted_volume_is_noop(expiry, ec2):
    ec2.describe_volumes.side_effect = botocore.exceptions.ClientError(
        {"Error": {"Code": "InvalidVolume.NotFound", "Message": "gone"}}, "DescribeVolumes")
    assert not expiry.delete_detached_volume_if_backed_up("vol-1", ENDED)
    ec2.delete_volume.assert_not_called()
