"""AWS backend: DynamoDB for state, SQS for async operations."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
import botocore.exceptions

from ..common.config import GpuDevConfig
from ..common.errors import GpuDevAuthError, GpuDevError
from ..common.models import DiskInfo, GpuAvailability, ReservationInfo, ReservationParams


_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "prod": {"region": "us-east-2"},
    "prod-east1": {"region": "us-east-1"},
}

_PREFIX = "pytorch-gpu-dev"


class AwsBackend:
    """Backend implementation using DynamoDB + SQS."""

    def __init__(self, config: GpuDevConfig) -> None:
        self._config = config
        region = config.region or _ENVIRONMENTS.get(config.environment, {}).get("region", "us-east-2")

        try:
            session = boto3.Session(profile_name="gpu-dev")
            session.get_credentials()
        except Exception:
            session = boto3.Session()

        self._session = session
        self._region = region
        self._ddb = session.resource("dynamodb", region_name=region)
        self._sqs = session.client("sqs", region_name=region)
        self._sts = session.client("sts", region_name=region)

        self._reservations = self._ddb.Table(f"{_PREFIX}-reservations")
        self._availability = self._ddb.Table(f"{_PREFIX}-gpu-availability")
        self._disks = self._ddb.Table(f"{_PREFIX}-disks")
        self._queue_url: str | None = None

    def _get_queue_url(self) -> str:
        if self._queue_url is None:
            self._queue_url = self._sqs.get_queue_url(
                QueueName=f"{_PREFIX}-reservation-queue"
            )["QueueUrl"]
        return self._queue_url

    def authenticate(self) -> dict[str, str]:
        try:
            identity = self._sts.get_caller_identity()
            arn = identity["Arn"]
            user_id = arn.split("/")[-1]
            return {
                "user_id": user_id,
                "github_user": self._config.github_user or "",
            }
        except Exception as e:
            raise GpuDevAuthError(f"Authentication failed: {e}")

    def create_reservation(self, params: dict) -> str:
        reservation_id = str(uuid.uuid4())
        message = {
            "reservation_id": reservation_id,
            "user_id": params["user_id"],
            "gpu_count": params.get("gpu_count", 1),
            "gpu_type": params.get("gpu_type", "a100"),
            "duration_hours": params.get("duration_hours", 8.0),
            "name": params.get("name"),
            "created_at": datetime.utcnow().isoformat(),
            "status": "pending",
            "jupyter_enabled": params.get("jupyter", False),
            "recreate_env": params.get("recreate_env", False),
            "no_persistent_disk": params.get("no_persistent_disk", False),
            "github_user": params.get("github_user", ""),
            "preserve_entrypoint": params.get("preserve_entrypoint", False),
            "version": "0.6.0",
        }
        if params.get("disk_name"):
            message["disk_name"] = params["disk_name"]
        if params.get("docker_image"):
            message["dockerimage"] = params["docker_image"]
        if params.get("spot"):
            message["spot"] = True

        self._sqs.send_message(
            QueueUrl=self._get_queue_url(),
            MessageBody=json.dumps(message),
        )
        return reservation_id

    def get_reservation(self, reservation_id: str, user_id: str) -> ReservationInfo | None:
        if len(reservation_id) >= 32:
            resp = self._reservations.get_item(Key={"reservation_id": reservation_id})
            item = resp.get("Item")
            if item:
                return self._item_to_info(item)
            return None

        resp = self._reservations.query(
            IndexName="UserIndex",
            KeyConditionExpression="user_id = :uid",
            FilterExpression="begins_with(reservation_id, :rid)",
            ExpressionAttributeValues={":uid": user_id, ":rid": reservation_id},
        )
        items = resp.get("Items", [])
        if len(items) == 1:
            return self._item_to_info(items[0])
        return None

    def list_reservations(
        self, user_id: str | None = None, statuses: list[str] | None = None,
    ) -> list[ReservationInfo]:
        if not user_id:
            return []
        resp = self._reservations.query(
            IndexName="UserIndex",
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
        )
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = self._reservations.query(
                IndexName="UserIndex",
                KeyConditionExpression="user_id = :uid",
                ExpressionAttributeValues={":uid": user_id},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

        results = [self._item_to_info(item) for item in items]
        if statuses:
            results = [r for r in results if r.status.value in statuses]
        return sorted(results, key=lambda r: r.created_at or "", reverse=True)

    def cancel_reservation(self, reservation_id: str, user_id: str) -> bool:
        message = {
            "type": "cancellation",
            "reservation_id": reservation_id,
            "user_id": user_id,
        }
        self._sqs.send_message(
            QueueUrl=self._get_queue_url(),
            MessageBody=json.dumps(message),
        )
        return True

    def extend_reservation(self, reservation_id: str, user_id: str, hours: float) -> bool:
        message = {
            "type": "extend",
            "reservation_id": reservation_id,
            "user_id": user_id,
            "extend_hours": hours,
        }
        self._sqs.send_message(
            QueueUrl=self._get_queue_url(),
            MessageBody=json.dumps(message),
        )
        return True

    def get_availability(self) -> dict[str, GpuAvailability]:
        resp = self._availability.scan()
        items = resp.get("Items", [])
        result: dict[str, GpuAvailability] = {}
        for item in items:
            gpu_type = str(item.get("gpu_type", ""))
            result[gpu_type] = GpuAvailability(
                gpu_type=gpu_type,
                available=int(item.get("available_gpus", 0)),
                total=int(item.get("total_gpus", 0)),
                max_reservable=int(item.get("max_reservable", 0)),
            )
        return result

    def list_disks(self, user_id: str) -> list[DiskInfo]:
        resp = self._disks.query(
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": user_id},
        )
        return [
            DiskInfo(
                name=str(item.get("disk_name", "")),
                size_gb=int(item.get("size_gb", 0)),
                snapshot_count=int(item.get("snapshot_count", 0)),
                in_use=bool(item.get("in_use", False)),
                reservation_id=str(item.get("reservation_id", "")) or None,
                is_deleted=bool(item.get("is_deleted", False)),
            )
            for item in resp.get("Items", [])
        ]

    def add_user(self, reservation_id: str, user_id: str, github_username: str) -> bool:
        message = {
            "type": "add_user",
            "reservation_id": reservation_id,
            "user_id": user_id,
            "github_username": github_username,
        }
        self._sqs.send_message(
            QueueUrl=self._get_queue_url(),
            MessageBody=json.dumps(message),
        )
        return True

    def poll_reservation_status(self, reservation_id: str) -> ReservationInfo | None:
        resp = self._reservations.get_item(Key={"reservation_id": reservation_id})
        item = resp.get("Item")
        return self._item_to_info(item) if item else None

    @staticmethod
    def _item_to_info(item: dict[str, Any]) -> ReservationInfo:
        return ReservationInfo(
            id=str(item.get("reservation_id", "")),
            status=item.get("status", "unknown"),
            gpu_type=str(item.get("gpu_type", "")),
            gpu_count=int(item.get("gpu_count", 1)),
            name=str(item.get("name", "")) or None,
            created_at=str(item.get("created_at", "")) or None,
            launched_at=str(item.get("launched_at", "")) or None,
            expires_at=str(item.get("expires_at", "")) or None,
            ssh_command=str(item.get("ssh_command", "")) or None,
            pod_name=str(item.get("pod_name", "")) or None,
            fqdn=str(item.get("fqdn", "")) or None,
            node_ip=str(item.get("node_ip", "")) or None,
            instance_type=str(item.get("instance_type", "")) or None,
            failure_reason=str(item.get("failure_reason", "")) or None,
            jupyter_url=str(item.get("jupyter_url", "")) or None,
            jupyter_enabled=bool(item.get("jupyter_enabled", False)),
            disk_name=str(item.get("disk_name", "")) or None,
            is_multinode=bool(item.get("is_multinode", False)),
            user_id=str(item.get("user_id", "")) or None,
        )
