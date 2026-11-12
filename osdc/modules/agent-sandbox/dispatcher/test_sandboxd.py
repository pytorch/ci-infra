"""Tests for the dispatcher's sandboxd client (sandboxd.py).

The golden bytes below were produced by the real protobuf runtime from sandboxd's own
spec/process/v1/process.proto (v1.0.10), so the hand-written codec is checked against the
wire format sandboxd speaks, not against itself. The gRPC call is exercised end to end
against a fake ProcessService on loopback.
"""

from concurrent import futures

import grpc
import pytest
import sandboxd

# ExecuteWithLimitsRequest{config{command: ["python3", "t.py"], env_vars: {A: "1"}, cwd: "/w"},
# stdout_limit_bytes: 1024, stderr_limit_bytes: 64, time_limit: 30s}
GOLDEN_REQUEST = bytes.fromhex("0a1b0a07707974686f6e330a04742e707912060a01411201311a022f7710800818402202081e")
# ExecuteWithLimitsResponse{exit_code: 137, stdout: '{"x": 1}\n', stderr: "oom",
# termination_reason: TIME_LIMIT_EXCEEDED, elapsed_duration: 3s, stdout_truncated: true}
GOLDEN_RESPONSE = bytes.fromhex("08890112097b2278223a20317d0a1a036f6f6d20022a0208033001")
# ExecuteWithLimitsResponse{exit_code: -1}
GOLDEN_NEGATIVE_EXIT = bytes.fromhex("08ffffffffffffffffff01")


def test_request_matches_the_protobuf_wire_format():
    assert sandboxd.encode_request(["python3", "t.py"], {"A": "1"}, "/w", 30, 1024, 64) == GOLDEN_REQUEST


def test_response_decodes_every_field_and_skips_unknown_ones():
    """Field 5 (elapsed_duration) is not read, so it doubles as the unknown-field case."""
    assert sandboxd.decode_response(GOLDEN_RESPONSE) == sandboxd.Execution(
        exit_code=137,
        stdout=b'{"x": 1}\n',
        stderr=b"oom",
        timed_out=True,
        stdout_truncated=True,
        stderr_truncated=False,
    )


def test_a_negative_exit_code_round_trips():
    assert sandboxd.decode_response(GOLDEN_NEGATIVE_EXIT).exit_code == -1


def test_an_empty_response_is_a_clean_exit():
    """proto3 omits defaults, so exit 0 with no output is zero bytes on the wire."""
    assert sandboxd.decode_response(b"") == sandboxd.Execution(0, b"", b"", False, False, False)


def test_unicode_env_is_encoded_as_utf8():
    request = sandboxd.encode_request(["t"], {"SANDBOX_TASK": "héllo ✓"}, "/w", 1, 1, 1)
    assert "héllo ✓".encode() in request


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        (b"\x08", "bad varint"),  # varint cut short
        (b"\x12\x05ab", "field 2"),  # length past the end
        (b"\x0b", "wire type 3"),  # group wire types are not protobuf3
        (b"\x0a\x01x", "field 1 is not a varint"),  # exit_code as bytes
        (b"\x10\x01", "field 2 is not bytes"),  # stdout as a varint
        (b"\x08" + b"\xff" * 10 + b"\x01", "bad varint"),  # varint over 64 bits
    ],
)
def test_a_malformed_reply_is_a_sandboxd_error(reply, match):
    """The reply comes from an untrusted pod; garbage must be a clean error, never a
    traceback through the dispatcher."""
    with pytest.raises(sandboxd.SandboxdError, match=match):
        sandboxd.decode_response(reply)


@pytest.fixture
def fake_sandboxd(monkeypatch, tmp_path):
    """A ProcessService on [::1] that records what it was sent and answers as told."""
    seen = {}
    behaviour = {"reply": GOLDEN_RESPONSE, "abort": None}

    def execute_with_limits(request: bytes, context):
        seen["request"] = request
        seen["metadata"] = dict(context.invocation_metadata())
        if behaviour["abort"]:
            context.abort(*behaviour["abort"])
        return behaviour["reply"]

    handler = grpc.method_handlers_generic_handler(
        "process.v1.ProcessService",
        {"ExecuteWithLimits": grpc.unary_unary_rpc_method_handler(execute_with_limits)},
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((handler,))
    port = server.add_insecure_port("[::1]:0")
    server.start()
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    monkeypatch.setattr(sandboxd, "PORT", port)
    monkeypatch.setattr(sandboxd, "TOKEN_PATH", token)
    yield seen, behaviour
    server.stop(None)


def _run(**overrides):
    kwargs = {
        "command": ["python3", "t.py"],
        "env": {"A": "1"},
        "cwd": "/w",
        "time_limit_s": 30,
        "stdout_limit": 1024,
        "stderr_limit": 64,
        "rpc_timeout_s": 10,
    }
    kwargs.update(overrides)
    return sandboxd.execute("::1", **kwargs)


def test_execute_sends_the_request_with_the_token_and_decodes_the_reply(fake_sandboxd):
    seen, _ = fake_sandboxd
    assert _run().exit_code == 137
    assert seen["request"] == GOLDEN_REQUEST
    assert seen["metadata"]["authorization"] == "Bearer s3cret", "the token file's whitespace must be stripped"


def test_a_refused_call_is_a_sandboxd_error(fake_sandboxd):
    _, behaviour = fake_sandboxd
    behaviour["abort"] = (grpc.StatusCode.UNAUTHENTICATED, "bad token")
    with pytest.raises(sandboxd.SandboxdError, match="UNAUTHENTICATED: bad token"):
        _run()


def test_an_unreachable_sandboxd_times_out_as_a_sandboxd_error(monkeypatch, tmp_path):
    """wait_for_ready keeps retrying a closed port until the RPC deadline, then gives up
    with an error rather than hanging."""
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setattr(sandboxd, "TOKEN_PATH", token)
    monkeypatch.setattr(sandboxd, "PORT", 1)  # nothing listens on port 1
    with pytest.raises(sandboxd.SandboxdError, match="DEADLINE_EXCEEDED"):
        _run(rpc_timeout_s=0.5)


def test_inputs_with_no_utf8_form_are_a_sandboxd_error(fake_sandboxd):
    """Not a UnicodeEncodeError, which the task loop does not catch: an uncaught one would
    skip recording the result and leave the task's slot taken for good."""
    seen, _ = fake_sandboxd
    with pytest.raises(sandboxd.SandboxdError, match="not valid UTF-8"):
        _run(env={"SANDBOX_TASK": "\ud800"})
    assert "request" not in seen


def test_a_missing_token_file_raises_before_any_call(monkeypatch, tmp_path):
    """OSError, which the task loop reports as a dispatch error."""
    monkeypatch.setattr(sandboxd, "TOKEN_PATH", tmp_path / "absent")
    with pytest.raises(FileNotFoundError):
        _run()


def test_an_ipv4_target_is_not_bracketed(monkeypatch):
    targets = []

    class Channel:
        def __init__(self, target, options):
            targets.append(target)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def unary_unary(self, method):
            return lambda request, **kw: b""

    monkeypatch.setattr(sandboxd.grpc, "insecure_channel", Channel)
    monkeypatch.setattr(sandboxd, "TOKEN_PATH", type("P", (), {"read_text": lambda self: "t"})())
    sandboxd.execute("10.0.0.5", **_kwargs())
    sandboxd.execute("fd00::5", **_kwargs())
    assert targets == ["10.0.0.5:9090", "[fd00::5]:9090"]


def _kwargs():
    return {
        "command": ["t"],
        "env": {},
        "cwd": "/w",
        "time_limit_s": 1,
        "stdout_limit": 1,
        "stderr_limit": 1,
        "rpc_timeout_s": 1,
    }
