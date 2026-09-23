"""deploy.sh's signing-key fetch: fatal only when the cluster has no keys to fall back on.

Runs the real block from deploy.sh under bash, with a fake `kubectl` on PATH.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parent.parent / "deploy.sh"

FAKE_KUBECTL = """#!/usr/bin/env bash
case "$1 $2" in
  "create job") exit "${FAKE_CREATE:-0}" ;;
  "wait --for=condition=complete") exit "${FAKE_WAIT:-0}" ;;
  "delete job") exit 0 ;;
  "get configmap")
    [[ "${FAKE_GET:-0}" == 0 ]] || exit "$FAKE_GET"
    printf '%s' "${FAKE_KEYS:-}" ;;
esac
"""


def keys(age_s: float = 3600, key_list=({"kty": "RSA", "kid": "k"},)) -> str:
    """A refresher-shaped document. Key material is not what deploy.sh checks (see the
    comment on JWKS_USABLE_PY), so the key here is a placeholder."""
    return json.dumps({"fetched_at": time.time() - age_s, "jwks": {"keys": list(key_list)}})


def _jwks_block() -> str:
    text = DEPLOY_SH.read_text()
    start = text.index("JWKS_USABLE_PY=")
    end = text.index("# --- Prune objects")
    return text[start:end]


def run_block(tmp_path, **fake) -> subprocess.CompletedProcess:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o755)
    script = "set -euo pipefail\nNAMESPACE=ai-sandbox\n" + _jwks_block() + "echo REACHED_END\n"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", **{k.upper(): v for k, v in fake.items()}}
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)


def test_a_successful_fetch_continues(tmp_path):
    done = run_block(tmp_path)
    assert done.returncode == 0
    assert "REACHED_END" in done.stdout


@pytest.mark.parametrize("failure", [{"fake_create": "1"}, {"fake_wait": "1"}], ids=["create", "wait"])
def test_a_failed_fetch_with_keys_in_place_warns_and_continues(tmp_path, failure):
    done = run_block(tmp_path, fake_keys=keys(age_s=3600), **failure)
    assert done.returncode == 0
    assert "Warning" in done.stdout
    assert "stay in use for about 1380 min" in done.stdout or "stay in use for about 1379 min" in done.stdout
    assert "REACHED_END" in done.stdout


@pytest.mark.parametrize(
    ("existing", "why"),
    [
        (keys(age_s=25 * 3600), "past the 24h limit"),
        (keys(age_s=23.5 * 3600), "expiring within the hour"),
        (keys(age_s=-3600), "in the future"),
        (keys(key_list=()), "no keys"),
        (json.dumps({"jwks": {"keys": [{"kid": "k"}]}}), "no fetched_at"),
        (json.dumps({"fetched_at": True, "jwks": {"keys": [{"kid": "k"}]}}), "no fetched_at"),
        ("[1]", "not a JSON object"),
        ("{not json", "not valid JSON"),
    ],
    ids=["stale", "nearly-stale", "future", "empty-set", "no-timestamp", "bool-timestamp", "list", "garbage"],
)
def test_a_failed_fetch_with_unusable_keys_stops_the_deploy(tmp_path, existing, why):
    done = run_block(tmp_path, fake_wait="1", fake_keys=existing)
    assert done.returncode == 1
    assert "cannot carry this deploy" in done.stderr
    assert why in done.stderr
    assert "REACHED_END" not in done.stdout


@pytest.mark.parametrize("failure", [{"fake_create": "1"}, {"fake_wait": "1"}], ids=["create", "wait"])
def test_a_failed_fetch_on_a_first_deploy_stops_the_deploy(tmp_path, failure):
    done = run_block(tmp_path, **failure)
    assert done.returncode == 1
    assert "holds no signing keys" in done.stderr
    assert "REACHED_END" not in done.stdout


def test_an_unreadable_configmap_is_not_mistaken_for_either_case(tmp_path):
    done = run_block(tmp_path, fake_wait="1", fake_get="1")
    assert done.returncode == 1
    assert "could not be read" in done.stderr
