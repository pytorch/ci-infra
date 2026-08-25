"""Verify a GitHub Actions OIDC token. Authenticity only — authorization is authorize.py.

This file answers exactly one question: did GitHub sign this token, for us, recently.
It never decides whether the caller may do anything.

Three deliberate choices, each of which has a cheaper wrong version:

PyJWT + cryptography, not a hand-rolled verifier. GitHub signs RS256, and the Python
standard library has no public-key crypto at all — no RSA anywhere. A from-scratch
RS256 check is about fifty lines, and the fifty-first is a Bleichenbacher signature
forgery: scanning the decrypted block for the 0x00 separator instead of comparing the
whole reconstructed padding byte for byte accepts forged signatures. That is precisely
the bug class an audited library is hardened against. The dispatcher image already links
OpenSSL through `ssl` for its Kubernetes client, so admitting `cryptography` does not
open a category that was previously closed.

The keys come from a mounted file, not from the network. The dispatcher is the component
holding create-Job RBAC, so it is the one that must not be able to reach the internet —
its NetworkPolicy allows DNS and the API server and nothing else. Something out of band
refreshes the ConfigMap this reads; see kubernetes/base/jwks.yaml.

Nothing is trusted from the token's own header except the key id. `algorithms=` is
passed explicitly so `alg: none` and the RS256-verified-as-HS256 confusion attack are
both rejected by the library rather than by us noticing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import jwt
from jwt import PyJWKSet

# PyJWT 1.x accepted a token with no `algorithms=` argument, which is the algorithm
# confusion attack with the safety catch off. Rather than trust that the base image ships
# 2.x, fail at import: a dispatcher that cannot verify safely must not start and answer
# /run at all.
if tuple(int(part) for part in jwt.__version__.split(".")[:1]) < (2,):
    raise RuntimeError(f"PyJWT >= 2 required for safe verification, found {jwt.__version__}")

ISSUER = "https://token.actions.githubusercontent.com"
# A custom audience, not the default (which is the repository owner URL). It binds the
# token to this recipient, so a token minted for some other service cannot be replayed
# here. It proves recipient, never authorization — that is authorize.py's job.
AUDIENCE = os.environ.get("OIDC_AUDIENCE", "agent-service")

# The refresher writes {"fetched_at": <unix seconds>, "jwks": {"keys": [...]}}.
#
# fetched_at is IN THE FILE rather than taken from its mtime, and that is not a detail:
# a ConfigMap volume is only updated when its content changes, and GitHub's signing keys
# change rarely. Judging freshness by mtime would mean a healthy, unchanged keyset aged
# out and the dispatcher refused every token — while a refresher that had been dead for
# a month, with the keys frozen, looked exactly the same. Writing the timestamp makes
# every successful refresh a content change, so the mount updates and this reads it.
JWKS_PATH = Path(os.environ.get("JWKS_PATH", "/etc/agent-sandbox/jwks/jwks.json"))
# A keyset older than this is refused. Refused rather than warned: stale key material is
# the input to every decision this file makes, and a silently dead refresher is exactly
# the failure this bound exists to convert into a loud one.
JWKS_MAX_AGE_S = int(os.environ.get("JWKS_MAX_AGE_S", str(24 * 3600)))
# The mounted file is re-read at most this often; a ConfigMap update lands within about a
# minute of the kubelet syncing it.
JWKS_RELOAD_INTERVAL_S = 60

# Claims without which authorize.py cannot make a decision. Required by the library, so
# an absent claim is a verification failure rather than a None flowing into a comparison.
REQUIRED_CLAIMS = ("iss", "aud", "exp", "iat", "repository_id", "repository_owner_id", "workflow_ref", "event_name")

_LOCK = threading.Lock()
_CACHE: dict = {"keyset": None, "loaded_at": 0.0, "fetched_at": None}


class InvalidToken(RuntimeError):
    """The token is absent, malformed, expired, or not signed by the issuer."""


def _load_keyset() -> PyJWKSet:
    """The mounted JWKS, re-read when the file changes or the interval elapses.

    Not cached forever: the ConfigMap is updated out of band, and a process that read it
    once at startup would keep rejecting valid tokens after the first key rotation.
    """
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE["keyset"]
        if cached is not None and now - _CACHE["loaded_at"] < JWKS_RELOAD_INTERVAL_S:
            return cached

        try:
            document = json.loads(JWKS_PATH.read_text())
        except OSError as exc:
            raise InvalidToken(f"no signing keys available: {exc}") from None
        except ValueError as exc:
            raise InvalidToken(f"signing keys are unreadable: {exc}") from None

        fetched_at = document.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            raise InvalidToken("signing key file carries no fetched_at")
        age = time.time() - fetched_at
        if age > JWKS_MAX_AGE_S:
            raise InvalidToken(
                f"signing keys are {int(age)}s old (max {JWKS_MAX_AGE_S}s) — the JWKS refresher has stopped"
            )

        try:
            keyset = PyJWKSet.from_dict(document["jwks"])
        except (ValueError, KeyError, TypeError, jwt.PyJWKSetError) as exc:
            # PyJWKSet raises for an empty or all-invalid key set too, so there is no
            # separate emptiness check below — adding one would be unreachable code
            # claiming to be a safety net.
            raise InvalidToken(f"signing keys are unreadable: {exc}") from None

        _CACHE.update(keyset=keyset, loaded_at=now, fetched_at=fetched_at)
        return keyset


def bearer_token(authorization_header: str | None) -> str:
    """The token out of an Authorization header, or raise."""
    if not authorization_header:
        raise InvalidToken("no Authorization header")
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise InvalidToken("Authorization header is not a bearer token")
    return token.strip()


def verify(token: str) -> dict:
    """Return the token's verified claims, or raise InvalidToken.

    Every check here is the library's, configured explicitly. In particular `algorithms`
    is a fixed list rather than anything read out of the token's own header.
    """
    keyset = _load_keyset()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"malformed token: {exc}") from None

    kid = header.get("kid")
    if not kid:
        raise InvalidToken("token header carries no kid")
    try:
        key = next(k for k in keyset.keys if k.key_id == kid)
    except StopIteration:
        # Not refreshed on demand: this process cannot reach GitHub, by design. An
        # unrecognised kid after a rotation is the refresher's problem to fix, and the
        # staleness bound above is what turns a dead refresher into a loud failure.
        raise InvalidToken(f"no signing key {kid!r} in the mounted key set") from None

    try:
        return jwt.decode(
            token,
            key=key.key,
            algorithms=["RS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": list(REQUIRED_CLAIMS), "verify_exp": True, "verify_aud": True, "verify_iss": True},
        )
    except jwt.PyJWTError as exc:
        # The reason goes to an unauthenticated caller, so it names the class of failure
        # (expired, bad audience) without echoing any of the token back.
        raise InvalidToken(f"token rejected: {type(exc).__name__}") from None
