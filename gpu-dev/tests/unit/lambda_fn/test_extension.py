"""Unit tests for reservation extension duration limits."""

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

_RESERVATION_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _record(user_id, extension_hours=24):
    return {
        "body": json.dumps(
            {
                "reservation_id": _RESERVATION_ID,
                "extension_hours": extension_hours,
                "user_id": user_id,
            }
        )
    }


def _reservation(user_id, gpu_count):
    return {
        "reservation_id": _RESERVATION_ID,
        "user_id": user_id,
        "status": "active",
        "gpu_type": "h100",
        "gpu_count": gpu_count,
        "launched_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "2026-01-31T00:00:00+00:00",
        "duration_hours": Decimal("720"),
    }


def _patch_extension_dependencies(lambda_index, monkeypatch, reservation):
    monkeypatch.setattr(
        lambda_index,
        "find_reservation_by_prefix",
        lambda reservation_id, user_id: reservation,
    )
    update_error = MagicMock()
    monkeypatch.setattr(lambda_index, "update_reservation_error", update_error)
    monkeypatch.setattr(lambda_index, "append_status_history", MagicMock())
    return update_error


@pytest.mark.parametrize("user_id", ["bobren@meta.com", "huydhn@meta.com"])
def test_allowlisted_single_gpu_extension_has_no_total_duration_limit(
    lambda_index, monkeypatch, aws_mocks, user_id
):
    reservation = _reservation(user_id, Decimal("1"))
    update_error = _patch_extension_dependencies(lambda_index, monkeypatch, reservation)

    assert lambda_index.process_extend_reservation_action(_record(user_id)) is True

    update_error.assert_not_called()
    update_call = aws_mocks["dynamodb"].Table.return_value.update_item.call_args.kwargs
    assert update_call["ExpressionAttributeValues"][":new_expires_at"] == (
        "2026-02-01T00:00:00+00:00"
    )
    assert update_call["ExpressionAttributeValues"][":new_duration"] == Decimal("744.0")


def test_non_allowlisted_single_gpu_extension_keeps_total_duration_limit(
    lambda_index, monkeypatch, aws_mocks
):
    reservation = _reservation("alice@meta.com", Decimal("1"))
    update_error = _patch_extension_dependencies(lambda_index, monkeypatch, reservation)

    assert lambda_index.process_extend_reservation_action(_record("alice@meta.com")) is True

    update_error.assert_called_once()
    assert "beyond 48 hours total" in update_error.call_args.args[1]
    aws_mocks["dynamodb"].Table.return_value.update_item.assert_not_called()


def test_allowlisted_multi_gpu_extension_keeps_total_duration_limit(
    lambda_index, monkeypatch, aws_mocks
):
    reservation = _reservation("bobren@meta.com", Decimal("8"))
    update_error = _patch_extension_dependencies(lambda_index, monkeypatch, reservation)

    assert lambda_index.process_extend_reservation_action(_record("bobren@meta.com")) is True

    update_error.assert_called_once()
    assert "beyond 48 hours total" in update_error.call_args.args[1]
    aws_mocks["dynamodb"].Table.return_value.update_item.assert_not_called()
