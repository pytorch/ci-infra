"""The JWKS refresher, including every way it is allowed to fail.

The failure paths matter more than the success path here. This job is the only thing
keeping the dispatcher's signing keys current, and the two ways it can go wrong are
opposite: overwrite good keys with bad ones (an immediate outage at the next reload), or
die quietly and leave the dispatcher trusting frozen key material. The tests below pin
both — a bad fetch must leave the ConfigMap untouched, and a good fetch must write a
timestamp so the dispatcher can tell the difference.
"""

from __future__ import annotations

import io
import json
import time

import jwks_refresh
import jwt
import kube
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


def _jwk(kid: str) -> dict:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "use": "sig"}


@pytest.fixture(scope="module")
def good_response() -> bytes:
    return json.dumps({"keys": [_jwk("kid-1"), _jwk("kid-2")]}).encode()


@pytest.fixture
def served(monkeypatch):
    """Serve a chosen body from the JWKS URL, and record what was requested."""
    state = {"body": b"", "error": None, "url": None}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def fake_urlopen(request, timeout=None, context=None):
        state["url"] = request.full_url
        if state["error"] is not None:
            raise state["error"]
        return Response(state["body"])

    monkeypatch.setattr(jwks_refresh.urllib.request, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def patched(monkeypatch):
    """Record the ConfigMap patch instead of making it."""
    calls = []
    monkeypatch.setattr(
        kube,
        "api_request",
        lambda method, path, body=None, raw=False, content_type="": calls.append(
            {"method": method, "path": path, "body": body, "content_type": content_type}
        ),
    )
    return calls


class TestFetch:
    def test_returns_the_published_keys(self, served, good_response):
        served["body"] = good_response
        assert len(jwks_refresh.fetch_jwks()["keys"]) == 2
        assert served["url"] == jwks_refresh.JWKS_URL

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (b'{"keys": []}', "no keys"),
            (b"{}", "no keys"),
            (b'{"keys": "not-a-list"}', "no keys"),
            (b'{"keys": [{"kty": "RSA"}]}', "no kid"),
        ],
        ids=["empty", "absent", "wrong-type", "no-kid"],
    )
    def test_rejects_anything_that_is_not_a_key_set(self, served, body, reason):
        served["body"] = body
        with pytest.raises(ValueError, match=reason):
            jwks_refresh.fetch_jwks()

    def test_rejects_a_key_set_the_dispatcher_could_not_load(self, served):
        """Shape-checking is not the property that matters. A structurally plausible key
        that the JWT library cannot parse would break the dispatcher at its next
        reload — with the working keys already overwritten."""
        served["body"] = json.dumps({"keys": [{"kty": "RSA", "kid": "broken", "n": "!!!", "e": "AQAB"}]}).encode()
        with pytest.raises((ValueError, jwks_refresh.PyJWKSetError)):
            jwks_refresh.fetch_jwks()


class TestWrite:
    def test_writes_a_merge_patch_carrying_a_fetch_timestamp(self, patched, good_response):
        before = time.time()
        jwks_refresh.write_configmap(json.loads(good_response))
        call = patched[0]

        assert call["method"] == "PATCH"
        assert call["path"].endswith(f"/configmaps/{jwks_refresh.CONFIGMAP_NAME}")
        # The API server rejects a PATCH sent as application/json; it has to be told
        # which patch dialect the body is in.
        assert call["content_type"] == "application/merge-patch+json"

        document = json.loads(call["body"]["data"][jwks_refresh.CONFIGMAP_KEY])
        assert len(document["jwks"]["keys"]) == 2
        assert document["fetched_at"] >= before, (
            "the timestamp is what makes an unchanged key set a content change, and so "
            "what makes a successful refresh visible to the dispatcher's mount at all"
        )


class TestMain:
    def test_success_reports_zero(self, served, patched, good_response):
        served["body"] = good_response
        assert jwks_refresh.main() == 0
        assert len(patched) == 1

    def test_a_failed_fetch_leaves_the_existing_keys_alone(self, served, patched):
        """Last-known-good is the point: writing nothing is strictly better than writing
        something unusable, and the dispatcher's staleness bound is what stops that
        being indefinite."""
        served["error"] = jwks_refresh.urllib.error.URLError("connection refused")
        assert jwks_refresh.main() == 1
        assert patched == []

    def test_an_unusable_response_leaves_the_existing_keys_alone(self, served, patched):
        served["body"] = b'{"keys": []}'
        assert jwks_refresh.main() == 1
        assert patched == []

    def test_a_failed_write_is_reported_rather_than_raised(self, served, monkeypatch, good_response):
        """A traceback out of a CronJob is a Job failure with the reason buried; this
        exits non-zero with a line naming the object it could not patch."""
        served["body"] = good_response

        def boom(*a, **kw):
            raise kube.ApiError("configmaps 'oidc-jwks' is forbidden")

        monkeypatch.setattr(kube, "api_request", boom)
        assert jwks_refresh.main() == 1
