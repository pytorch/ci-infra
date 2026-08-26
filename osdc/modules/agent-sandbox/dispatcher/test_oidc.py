"""Token verification, against real signatures.

Every token here is genuinely signed with a generated RSA key and verified through the
same code path production uses. Hand-built claim dicts would test the shape of the
result and none of the crypto, and the attacks worth testing — alg confusion, a swapped
key, a stale key set — are all invisible to that.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import oidc
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KID = "test-key-1"
OTHER_KID = "test-key-2"


def _keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def keys():
    return {KID: _keypair(), OTHER_KID: _keypair()}


def _jwks_document(keys, fetched_at=None):
    return {
        "fetched_at": time.time() if fetched_at is None else fetched_at,
        "jwks": {
            "keys": [
                {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "use": "sig"}
                for kid, key in keys.items()
            ]
        },
    }


@pytest.fixture
def jwks_file(tmp_path, keys, monkeypatch):
    """A mounted key set, as the refresher writes it."""
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(_jwks_document(keys)))
    monkeypatch.setattr(oidc, "JWKS_PATH", path)
    oidc._CACHE.update(keyset=None, loaded_at=0.0, fetched_at=None)
    return path


def a_token(keys, kid=KID, **overrides):
    claims = {
        "iss": oidc.ISSUER,
        "aud": oidc.AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "repository_id": "1133856973",
        "repository_owner_id": "21003710",
        "workflow_ref": "pytorch/ciforge/.github/workflows/x.yml@refs/heads/main",
        "event_name": "workflow_run",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, keys[kid], algorithm="RS256", headers={"kid": kid})


class TestBearerHeader:
    @pytest.mark.parametrize(
        "header", [None, "", "Basic abc", "Bearer", "Bearer    "], ids=["none", "empty", "basic", "bare", "blank"]
    )
    def test_rejects_anything_that_is_not_a_bearer_token(self, header):
        with pytest.raises(oidc.InvalidToken):
            oidc.bearer_token(header)

    def test_accepts_a_bearer_token_case_insensitively(self):
        assert oidc.bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


class TestVerify:
    def test_a_real_token_verifies(self, keys, jwks_file):
        claims = oidc.verify(a_token(keys))
        assert claims["repository_id"] == "1133856973"

    def test_a_token_signed_by_a_key_not_in_the_set_is_rejected(self, keys, jwks_file):
        stranger = _keypair()
        token = jwt.encode({"iss": oidc.ISSUER}, stranger, algorithm="RS256", headers={"kid": "unknown"})
        with pytest.raises(oidc.InvalidToken, match="no signing key"):
            oidc.verify(token)

    def test_a_token_signed_by_the_wrong_key_in_the_set_is_rejected(self, keys, jwks_file):
        """The kid names one key and the signature is another's — the check has to be the
        signature, not the kid the attacker chose."""
        token = jwt.encode({"iss": oidc.ISSUER}, keys[OTHER_KID], algorithm="RS256", headers={"kid": KID})
        with pytest.raises(oidc.InvalidToken):
            oidc.verify(token)

    def test_alg_none_is_rejected(self, keys, jwks_file):
        """The classic. Passing algorithms= explicitly is what makes this the library's
        problem rather than ours."""
        token = jwt.encode({"iss": oidc.ISSUER, "aud": oidc.AUDIENCE}, key=None, algorithm="none")
        with pytest.raises(oidc.InvalidToken):
            oidc.verify(token)

    def test_an_hs256_token_signed_with_the_public_key_is_rejected(self, keys, jwks_file):
        """Algorithm confusion: the public key is not a secret, so if HS256 were accepted
        anyone could mint a valid token from published key material.

        Assembled by hand, because PyJWT refuses to ENCODE this — which is a hint about
        how bad it is, and not a substitute for checking that we refuse to DECODE it."""
        public_pem = (
            keys[KID]
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        def segment(payload: bytes) -> bytes:
            return base64.urlsafe_b64encode(payload).rstrip(b"=")

        signing_input = b".".join(
            (
                segment(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode()),
                segment(json.dumps({"iss": oidc.ISSUER, "aud": oidc.AUDIENCE}).encode()),
            )
        )
        forged = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
        with pytest.raises(oidc.InvalidToken):
            oidc.verify((signing_input + b"." + segment(forged)).decode())

    def test_an_expired_token_is_rejected(self, keys, jwks_file):
        with pytest.raises(oidc.InvalidToken):
            oidc.verify(a_token(keys, exp=int(time.time()) - 60))

    def test_a_token_for_another_audience_is_rejected(self, keys, jwks_file):
        """The audience is what stops a token minted for some other service being
        replayed here."""
        with pytest.raises(oidc.InvalidToken):
            oidc.verify(a_token(keys, aud="https://github.com/pytorch"))

    def test_a_token_from_another_issuer_is_rejected(self, keys, jwks_file):
        with pytest.raises(oidc.InvalidToken):
            oidc.verify(a_token(keys, iss="https://evil.example/oidc"))

    @pytest.mark.parametrize("claim", ["repository_id", "repository_owner_id", "workflow_ref", "event_name"])
    def test_a_missing_required_claim_is_a_verification_failure(self, keys, jwks_file, claim):
        """Absent must fail here, not flow into authorize.py as a None that compares
        unequal to everything and happens to be denied for the wrong reason."""
        with pytest.raises(oidc.InvalidToken):
            oidc.verify(a_token(keys, **{claim: None}))

    def test_a_token_with_no_kid_is_rejected(self, keys, jwks_file):
        token = jwt.encode({"iss": oidc.ISSUER}, keys[KID], algorithm="RS256")
        with pytest.raises(oidc.InvalidToken, match="no kid"):
            oidc.verify(token)

    def test_garbage_is_rejected_without_raising_something_else(self, keys, jwks_file):
        with pytest.raises(oidc.InvalidToken):
            oidc.verify("not-a-token")


class TestKeySetHandling:
    def test_a_stale_key_set_refuses_every_token(self, keys, jwks_file, monkeypatch):
        """A refresher that dies silently would otherwise leave the dispatcher trusting a
        frozen key list for the life of the pod."""
        jwks_file.write_text(json.dumps(_jwks_document(keys, fetched_at=time.time() - oidc.JWKS_MAX_AGE_S - 1)))
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        with pytest.raises(oidc.InvalidToken, match="refresher has stopped"):
            oidc.verify(a_token(keys))

    def test_freshness_comes_from_the_file_not_its_mtime(self, keys, jwks_file):
        """A ConfigMap volume only updates when its CONTENT changes, and GitHub's keys
        change rarely — so an mtime-based bound would age out a perfectly healthy key
        set. The refresher writes fetched_at for exactly this reason."""
        import os

        old = time.time() - 10 * 24 * 3600
        os.utime(jwks_file, (old, old))
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        assert oidc.verify(a_token(keys))["event_name"] == "workflow_run"

    def test_a_missing_key_file_refuses_rather_than_skipping_verification(self, tmp_path, monkeypatch, keys):
        monkeypatch.setattr(oidc, "JWKS_PATH", tmp_path / "absent.json")
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        with pytest.raises(oidc.InvalidToken, match="no signing keys"):
            oidc.verify(a_token(keys))

    def test_an_empty_key_set_refuses(self, tmp_path, monkeypatch, keys):
        path = tmp_path / "jwks.json"
        path.write_text(json.dumps({"fetched_at": time.time(), "jwks": {"keys": []}}))
        monkeypatch.setattr(oidc, "JWKS_PATH", path)
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        with pytest.raises(oidc.InvalidToken, match="unreadable"):
            oidc.verify(a_token(keys))

    def test_a_key_file_with_no_timestamp_refuses(self, tmp_path, monkeypatch, keys):
        path = tmp_path / "jwks.json"
        path.write_text(json.dumps({"jwks": {"keys": []}}))
        monkeypatch.setattr(oidc, "JWKS_PATH", path)
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        with pytest.raises(oidc.InvalidToken, match="fetched_at"):
            oidc.verify(a_token(keys))

    def test_a_rotated_key_is_picked_up_without_a_restart(self, keys, jwks_file, monkeypatch):
        """The keys are re-read on an interval; a process that read them once at startup
        would reject every token after the first rotation."""
        fresh = _keypair()
        jwks_file.write_text(json.dumps(_jwks_document({"rotated": fresh})))
        monkeypatch.setattr(oidc, "JWKS_RELOAD_INTERVAL_S", 0)
        token = jwt.encode(
            {
                "iss": oidc.ISSUER,
                "aud": oidc.AUDIENCE,
                "exp": int(time.time()) + 300,
                "iat": int(time.time()),
                "repository_id": "1",
                "repository_owner_id": "2",
                "workflow_ref": "a/b/.github/workflows/c.yml@refs/heads/main",
                "event_name": "push",
            },
            fresh,
            algorithm="RS256",
            headers={"kid": "rotated"},
        )
        assert oidc.verify(token)["event_name"] == "push"

    def test_a_key_set_timestamped_in_the_future_refuses(self, keys, jwks_file):
        """A broken clock or a tampered file reads as "brand new" to an age check, which
        is the wrong direction to fail in."""
        jwks_file.write_text(json.dumps(_jwks_document(keys, fetched_at=time.time() + 10 * 3600)))
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        with pytest.raises(oidc.InvalidToken, match="future"):
            oidc.verify(a_token(keys))

    def test_a_cached_key_set_still_ages_out(self, keys, jwks_file, monkeypatch):
        """Checking the age only when the file is read would leave the keys usable for a
        whole reload interval past the bound — the one window the bound exists to close."""
        assert oidc.verify(a_token(keys))  # populates the cache
        monkeypatch.setattr(oidc, "JWKS_MAX_AGE_S", -1)
        with pytest.raises(oidc.InvalidToken, match="refresher has stopped"):
            oidc.verify(a_token(keys))

    def test_the_key_set_is_not_re_read_on_every_request(self, keys, jwks_file):
        """Each in-flight task's caller hits verify(); re-reading and re-parsing the
        mounted file every time is pure overhead on the request path."""
        assert oidc.verify(a_token(keys))
        jwks_file.unlink()
        assert oidc.verify(a_token(keys))["event_name"] == "workflow_run", (
            "the second call must be served from the cache, not from the file"
        )

    def test_an_unparseable_key_file_refuses(self, tmp_path, monkeypatch, keys):
        """A half-written or corrupted mount must fail closed, not raise something no
        caller catches."""
        path = tmp_path / "jwks.json"
        path.write_text("{ not json")
        monkeypatch.setattr(oidc, "JWKS_PATH", path)
        oidc._CACHE.update(keyset=None, loaded_at=0.0)
        with pytest.raises(oidc.InvalidToken, match="unreadable"):
            oidc.verify(a_token(keys))
