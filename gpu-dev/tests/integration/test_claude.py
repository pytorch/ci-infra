"""Integration: Claude Code runs inside the pod and answers a prompt.

The gpu-dev image ships Claude Code (auth'd via the pod's Bedrock IRSA perms).
This proves a fresh pod can start `claude` headless and get a real model response.

reserve cpu -> `claude -p '...'` -> expect "Paris". Skipped unless
--run-integration; skips (not fails) if SSH is unreachable from the runner or if
claude isn't on the image.
"""
import pytest

from .conftest import reserved, exec_or_skip

pytestmark = pytest.mark.integration

_PROMPT = "What is the capital of France? Answer with one word only."


def test_claude_answers_capital_of_france(manager):
    with reserved(manager, gpu_type="cpu-x86", gpu_count=0, hours=0.5) as (rid, conn):
        # Is claude even on this image? skip (don't fail) if not.
        rc, probe = exec_or_skip(conn, "command -v claude >/dev/null 2>&1 && echo HAVE_CLAUDE || echo NO_CLAUDE")
        if "NO_CLAUDE" in probe:
            pytest.skip("claude not installed in the pod image")

        # Headless one-shot prompt. Give the model call generous time.
        rc, out = exec_or_skip(
            conn,
            "claude -p " + _q(_PROMPT) + " 2>&1",
            timeout=240)
        low = out.lower()
        if rc != 0 and ("not found" in low or "command not found" in low):
            pytest.skip(f"claude unavailable: {out.strip()[:160]}")
        assert rc == 0, f"claude exited {rc}: {out}"
        assert "paris" in low, f"expected 'Paris' in claude output, got: {out}"


def _q(s: str) -> str:
    import shlex
    return shlex.quote(s)
