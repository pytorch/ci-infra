"""Shared pytest config + fixtures for the gpu-dev test suite.

Sets safe AWS / lambda env BEFORE any boto3-importing module loads, makes the
reservation_processor lambda importable as ``index``, and gates the
``integration`` marker (real-pod tests on staging) behind ``--run-integration``
(or ``GPU_DEV_RUN_INTEGRATION=1``).
"""
import os
import sys
import pathlib

# --- env: set before importing the lambda / any boto3 module --------------------
_ENV = {
    "AWS_DEFAULT_REGION": "us-east-2",
    "AWS_REGION": "us-east-2",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "REGION": "us-east-2",
    "RESERVATIONS_TABLE": "pytorch-gpu-dev-reservations",
    "QUEUE_URL": "https://sqs.us-east-2.amazonaws.com/000000000000/test-queue",
    "EKS_CLUSTER_NAME": "test-cluster",
    "PRIMARY_AVAILABILITY_ZONE": "us-east-2a",
    "MAX_RESERVATION_HOURS": "48",
    "DEFAULT_TIMEOUT_HOURS": "24",
    "WARM_POOL_TARGETS": '{"h100": 1, "b200": 1}',
}
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)

_ROOT = pathlib.Path(__file__).resolve().parent
_LAMBDA = _ROOT / "terraform-gpu-devservers" / "lambda"
for _p in (str(_LAMBDA), str(_LAMBDA / "reservation_processor")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration", action="store_true", default=False,
        help="Run integration tests that reserve real (staging) pods.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: real-pod test (staging cpu/t4); opt-in")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration") or os.environ.get("GPU_DEV_RUN_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(
        reason="integration test — pass --run-integration or set GPU_DEV_RUN_INTEGRATION=1")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


# --- shared fixtures ------------------------------------------------------------
@pytest.fixture
def cli_runner():
    """Click CliRunner for invoking gpu-dev commands in-process."""
    from click.testing import CliRunner
    return CliRunner()


@pytest.fixture
def lambda_index():
    """The reservation_processor lambda module (env already mocked)."""
    import index
    return index


@pytest.fixture
def aws_mocks(monkeypatch):
    """Replace the lambda's module-level boto3 handles with MagicMocks.

    Returns a dict {name: MagicMock} for dynamodb / sqs_client / eks_client /
    efs_client / autoscaling_client so tests can set return values + assert calls.
    """
    from unittest.mock import MagicMock
    import index
    mocks = {}
    for name in ("dynamodb", "sqs_client", "eks_client", "efs_client", "autoscaling_client"):
        if hasattr(index, name):
            m = MagicMock(name=name)
            monkeypatch.setattr(index, name, m, raising=False)
            mocks[name] = m
    return mocks
