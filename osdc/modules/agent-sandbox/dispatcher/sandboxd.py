"""Run one command in a task pod through its sandboxd, and return what it printed.

sandboxd (kubernetes-sigs/agent-sandbox, packages/sandboxd) is the daemon every task pod
runs. The dispatcher makes exactly one call into it: ProcessService.ExecuteWithLimits,
which starts a process, waits for it under a time limit, and returns its exit code and
bounded stdout/stderr in one response. If the call is cancelled or its connection drops,
sandboxd kills the process group — so a dispatcher that dies takes its task with it
instead of leaving it to be replayed.

Hand-encoded protobuf rather than generated stubs, deliberately: generated code is tied
to the protobuf runtime of the generator, and the dispatcher takes its packages from apt
(python3-grpcio), not pip. Two small messages are cheaper to encode than that coupling.
Field numbers follow spec/process/v1/process.proto at the pinned release (v1.0.10, the
same release kubernetes/controller pins); unknown response fields are skipped, so a newer
sandboxd adding fields does not break this.

No TLS: sandboxd does not terminate it. The call is authenticated with the bearer token
whose digest the task pod was started with, and only the dispatcher can reach :9090
(networkpolicy.yaml, sandbox-task-ingress).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import grpc

METHOD = "/process.v1.ProcessService/ExecuteWithLimits"
PORT = 9090
# The sandboxd-token Secret, mounted by dispatcher.yaml. Read per call so a rotated token
# is picked up without a restart.
TOKEN_PATH = Path(os.environ.get("SANDBOXD_TOKEN_FILE", "/var/run/secrets/sandboxd/token"))
# Room for the largest stdout and stderr the caller can ask for, plus framing.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# ExecuteTerminationReason.EXECUTE_TERMINATION_REASON_TIME_LIMIT_EXCEEDED
TIME_LIMIT_EXCEEDED = 2


class SandboxdError(RuntimeError):
    """The call did not produce a result: unreachable, refused, or a malformed reply."""


@dataclass(frozen=True)
class Execution:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


def _varint(n: int) -> bytes:
    if n < 0:  # int32/int64 negatives are encoded as their 64-bit two's complement
        n += 1 << 64
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        if not n:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _bytes_field(number: int, data: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def _varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def encode_request(
    command: list[str], env: dict[str, str], cwd: str, time_limit_s: int, stdout_limit: int, stderr_limit: int
) -> bytes:
    """ExecuteWithLimitsRequest{config: ProcessConfig{command, env_vars, cwd}, limits, time_limit}."""
    config = b"".join(_bytes_field(1, arg.encode()) for arg in command)
    for key, value in env.items():  # map<string,string> = repeated {1: key, 2: value}
        config += _bytes_field(2, _bytes_field(1, key.encode()) + _bytes_field(2, value.encode()))
    config += _bytes_field(3, cwd.encode())
    time_limit = _varint_field(1, time_limit_s)  # google.protobuf.Duration.seconds
    return (
        _bytes_field(1, config)
        + _varint_field(2, stdout_limit)
        + _varint_field(3, stderr_limit)
        + _bytes_field(4, time_limit)
    )


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise SandboxdError("malformed response: bad varint")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def decode_response(buf: bytes) -> Execution:
    """ExecuteWithLimitsResponse. Unknown fields are skipped; known ones must be well typed."""
    fields: dict[int, int | bytes] = {}
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            fields[number], i = _read_varint(buf, i)
            continue
        size = {1: 8, 5: 4}.get(wire)
        if wire == 2:
            size, i = _read_varint(buf, i)
        if size is None or i + size > len(buf):
            raise SandboxdError(f"malformed response: field {number} (wire type {wire})")
        fields[number] = buf[i : i + size]
        i += size

    def varint(number: int) -> int:
        value = fields.get(number, 0)
        if not isinstance(value, int):
            raise SandboxdError(f"malformed response: field {number} is not a varint")
        return value

    def data(number: int) -> bytes:
        value = fields.get(number, b"")
        if not isinstance(value, bytes):
            raise SandboxdError(f"malformed response: field {number} is not bytes")
        return value

    exit_code = varint(1)
    if exit_code >= 1 << 63:
        exit_code -= 1 << 64
    return Execution(
        exit_code=exit_code,
        stdout=data(2),
        stderr=data(3),
        timed_out=varint(4) == TIME_LIMIT_EXCEEDED,
        stdout_truncated=bool(varint(6)),
        stderr_truncated=bool(varint(7)),
    )


def execute(
    pod_ip: str,
    *,
    command: list[str],
    env: dict[str, str],
    cwd: str,
    time_limit_s: int,
    stdout_limit: int,
    stderr_limit: int,
    rpc_timeout_s: float,
) -> Execution:
    """Run `command` in the pod at `pod_ip` and wait for it. Raises SandboxdError.

    wait_for_ready covers the gap between the claim reporting Ready and sandboxd's gRPC
    listener accepting; the RPC timeout bounds that wait together with the run.
    """
    target = f"[{pod_ip}]:{PORT}" if ":" in pod_ip else f"{pod_ip}:{PORT}"
    token = TOKEN_PATH.read_text().strip()
    try:
        request = encode_request(command, env, cwd, time_limit_s, stdout_limit, stderr_limit)
    except UnicodeEncodeError as exc:  # a lone surrogate, which JSON input can carry
        raise SandboxdError(f"the task's inputs are not valid UTF-8: {exc.reason}") from None
    options = [("grpc.max_receive_message_length", MAX_RESPONSE_BYTES)]
    with grpc.insecure_channel(target, options=options) as channel:
        call = channel.unary_unary(METHOD)  # no (de)serializers: bytes in, bytes out
        try:
            reply = call(
                request,
                timeout=rpc_timeout_s,
                metadata=[("authorization", f"Bearer {token}")],
                wait_for_ready=True,
            )
        except grpc.RpcError as exc:
            raise SandboxdError(f"sandboxd at {target}: {exc.code().name}: {exc.details()}") from None
    return decode_response(reply)
