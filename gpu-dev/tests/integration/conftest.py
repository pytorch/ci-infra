"""Integration-test harness — drives REAL reservations on a live cluster.

All tests here are marked ``integration`` (skipped unless ``--run-integration`` /
``GPU_DEV_RUN_INTEGRATION=1``). They target the env named by ``GPU_DEV_TEST_ENV``
(default ``staging``) via the CLI's ReservationManager, and SKIP cleanly if that
env isn't reachable (e.g. staging not deployed). Every reservation is cancelled in
a finally, so a failed test never leaks a pod.

Run (once staging is applied):
    GPU_DEV_TEST_ENV=staging GPU_DEV_GITHUB_USER=<you> \
        pytest -m integration --run-integration -q
"""
import os
import time
import contextlib
import subprocess

import pytest

TEST_ENV = os.environ.get("GPU_DEV_TEST_ENV", "staging")
ACTIVE_TIMEOUT_MIN = float(os.environ.get("GPU_DEV_TEST_TIMEOUT_MIN", "15"))


@pytest.fixture(scope="session", autouse=True)
def _select_env():
    # Point the CLI Config at the target environment (staging by default) and PIN
    # the region to that env's region — the root conftest sets AWS_DEFAULT_REGION
    # to us-east-2 for the lambda unit tests, which would otherwise leak in and
    # send integration reservations to the wrong region.
    os.environ["GPU_DEV_ENVIRONMENT"] = TEST_ENV
    from gpu_dev_cli.config import Config
    region = Config.ENVIRONMENTS.get(TEST_ENV, {}).get("region")
    if region:
        os.environ["AWS_REGION"] = region
        os.environ["AWS_DEFAULT_REGION"] = region
    yield


@pytest.fixture(scope="session")
def manager():
    """CLI ReservationManager for the target env; skips if unreachable/unauth'd."""
    from gpu_dev_cli.config import load_config
    from gpu_dev_cli.reservations import ReservationManager
    from gpu_dev_cli.auth import authenticate_user

    cfg = load_config()
    try:
        # Reachability via the REAL reservation path (the reservation role may lack
        # DescribeTable but still allow SQS + DynamoDB query/put): the SQS queue must
        # resolve and the reservations table must be queryable.
        cfg.get_queue_url()
        cfg.dynamodb.Table(cfg.reservations_table).scan(Limit=1)
        user = authenticate_user(cfg)
    except Exception as e:  # not deployed / no creds / no github_user
        pytest.skip(f"env '{TEST_ENV}' not reachable ({cfg.reservations_table} @ {cfg.aws_region}): {e}")

    mgr = ReservationManager(cfg)
    mgr._test_user = user
    return mgr


def _ssh_exec(ssh_command: str, remote: str, timeout: int = 120):
    """Run a command on the pod via its ssh_command; returns (rc, output)."""
    if not ssh_command or ssh_command.startswith("ssh user@"):
        raise RuntimeError(f"no usable ssh_command: {ssh_command!r}")
    if "StrictHostKeyChecking" not in ssh_command:
        ssh_command = ssh_command.replace(
            "ssh ",
            "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR ",
            1)
    import shlex
    p = subprocess.run(f"{ssh_command} {shlex.quote(remote)}", shell=True,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)


# Output signatures of an environment that simply can't reach the pod over SSH
# (CI/sandbox with no outbound SSH, no agent key, no proxy) — distinct from a pod
# that's actually broken. We skip the exec assertion on these, not fail.
_SSH_UNREACHABLE = (
    "operation not permitted", "connection failed", "connection refused",
    "could not resolve", "connection timed out", "no route to host",
    "permission denied (publickey", "proxycommand", "broken pipe",
)


def exec_or_skip(conn: dict, remote: str, timeout: int = 120):
    """exec on the pod; skip (not fail) if SSH can't be reached from here."""
    ssh = conn.get("ssh_command")
    if not ssh:
        pytest.skip("reservation has no ssh_command")
    try:
        rc, out = _ssh_exec(ssh, remote, timeout=timeout)
    except Exception as e:
        pytest.skip(f"SSH exec unavailable from this environment: {e}")
    low = out.lower()
    if rc != 0 and any(s in low for s in _SSH_UNREACHABLE):
        pytest.skip(f"SSH not reachable from this environment: {out.strip()[:160]}")
    return rc, out


@contextlib.contextmanager
def reserved(manager, gpu_type, gpu_count, hours=0.5, **create_kwargs):
    """Reserve -> wait active -> yield (rid, connection info) -> ALWAYS cancel.

    Extra create_kwargs (e.g. dockerimage=...) pass straight to create_reservation.
    """
    user_id = manager._test_user["user_id"]
    github_user = manager._test_user["github_user"]
    rid = manager.create_reservation(
        user_id=user_id, gpu_count=gpu_count, gpu_type=gpu_type,
        duration_hours=hours, name="itest", github_user=github_user,
        no_persistent_disk=True, **create_kwargs)
    assert rid, "create_reservation returned no id"
    try:
        manager.wait_for_reservation_completion(
            reservation_id=rid, timeout_minutes=ACTIVE_TIMEOUT_MIN, verbose=False)
        conn = manager.get_connection_info(rid, user_id) or {}
        yield rid, conn
    finally:
        with contextlib.suppress(Exception):
            manager.cancel_reservation(rid, user_id)


def read_reservation(manager, rid: str) -> dict:
    """Fetch the raw reservation record from DynamoDB (for warm_claimed, status…)."""
    from gpu_dev_cli.config import load_config
    cfg = load_config()
    item = cfg.dynamodb.Table(cfg.reservations_table).get_item(
        Key={"reservation_id": rid}).get("Item")
    return item or {}
