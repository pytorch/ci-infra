"""Regression tests for non-blocking snapshot waits and stale SQS retries."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import botocore.exceptions
import pytest


def test_pending_snapshot_returns_progress_without_waiting(monkeypatch, lambda_index):
    ec2 = MagicMock()
    ec2.describe_volumes.return_value = {"Volumes": []}
    ec2.describe_snapshots.return_value = {
        "Snapshots": [
            {
                "SnapshotId": "snap-pending",
                "StartTime": datetime.now(timezone.utc),
                "Progress": "37%",
            }
        ]
    }
    monkeypatch.setattr(lambda_index, "ec2_client", ec2)

    with pytest.raises(
        lambda_index.SnapshotPendingError,
        match=r"snap-pending.*37%",
    ):
        lambda_index.create_disk_from_snapshot_or_empty(
            "user@example.com", "us-east-2a", "default", "reservation-id"
        )

    ec2.get_waiter.assert_not_called()


def test_pending_clone_snapshot_returns_progress_without_waiting(
    monkeypatch, lambda_index, aws_mocks
):
    ec2 = MagicMock()
    ec2.describe_volumes.return_value = {"Volumes": []}
    ec2.describe_snapshots.side_effect = [
        {"Snapshots": []},
        {
            "Snapshots": [
                {
                    "SnapshotId": "snap-clone",
                    "StartTime": datetime.now(timezone.utc),
                    "State": "pending",
                    "Progress": "52%",
                }
            ]
        },
    ]
    ec2.get_paginator.return_value.paginate.return_value = [{"Snapshots": []}]
    monkeypatch.setattr(lambda_index, "ec2_client", ec2)
    disk_table = MagicMock()
    disk_table.get_item.return_value = {
        "Item": {"clone_source_snapshot": "snap-clone"}
    }
    aws_mocks["dynamodb"].Table.return_value = disk_table

    with pytest.raises(
        lambda_index.SnapshotPendingError,
        match=r"snap-clone.*52%",
    ):
        lambda_index.create_disk_from_snapshot_or_empty(
            "user@example.com", "us-east-2a", "clone", "reservation-id"
        )

    ec2.get_waiter.assert_not_called()


def test_cancelled_reservation_ignores_stale_sqs_retry(lambda_index, aws_mocks):
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "reservation_id": "cancelled-reservation",
            "status": "cancelled",
            "cancelled_at": "2026-08-17T16:27:45",
        }
    }
    aws_mocks["dynamodb"].Table.return_value = table

    result = lambda_index.process_reservation_request(
        {
            "body": json.dumps(
                {
                    "reservation_id": "cancelled-reservation",
                    "user_id": "user@example.com",
                    "gpu_count": 1,
                    "gpu_type": "b200",
                }
            )
        }
    )

    assert result is True
    table.put_item.assert_not_called()


def test_cancellation_wins_atomic_initial_record_race(
    monkeypatch, lambda_index, aws_mocks
):
    table = MagicMock()
    table.get_item.return_value = {}
    table.put_item.side_effect = botocore.exceptions.ClientError(
        {
            "Error": {
                "Code": "ConditionalCheckFailedException",
                "Message": "reservation was cancelled",
            }
        },
        "PutItem",
    )
    aws_mocks["dynamodb"].Table.return_value = table
    validate = MagicMock(return_value=(True, ""))
    monkeypatch.setattr(lambda_index, "validate_reservation_request", validate)

    result = lambda_index.process_reservation_request(
        {
            "body": json.dumps(
                {
                    "reservation_id": "raced-reservation",
                    "user_id": "user@example.com",
                    "gpu_count": 1,
                    "gpu_type": "b200",
                }
            )
        }
    )

    assert result is True
    validate.assert_not_called()


def test_initial_snapshot_wait_moves_reservation_to_queue(
    monkeypatch, lambda_index, aws_mocks
):
    table = MagicMock()
    table.get_item.return_value = {}
    aws_mocks["dynamodb"].Table.return_value = table
    monkeypatch.setattr(
        lambda_index, "validate_reservation_request", lambda _request: (True, "")
    )
    monkeypatch.setattr(lambda_index, "check_gpu_availability", lambda _gpu_type: 1)
    monkeypatch.setattr(
        lambda_index, "check_max_gpus_on_single_node", lambda _gpu_type: 1
    )
    monkeypatch.setattr(
        lambda_index, "create_reservation", lambda request: request["reservation_id"]
    )
    monkeypatch.setattr(
        lambda_index,
        "allocate_gpu_resources",
        MagicMock(side_effect=lambda_index.SnapshotPendingError("snapshot is 37%")),
    )
    update_status = MagicMock()
    monkeypatch.setattr(lambda_index, "update_reservation_status", update_status)

    result = lambda_index.process_reservation_request(
        {
            "body": json.dumps(
                {
                    "reservation_id": "waiting-reservation",
                    "user_id": "user@example.com",
                    "gpu_count": 1,
                    "gpu_type": "b200",
                    "duration_hours": 1,
                }
            )
        }
    )

    assert result is True
    update_status.assert_any_call(
        "waiting-reservation", "queued", "snapshot is 37%"
    )
