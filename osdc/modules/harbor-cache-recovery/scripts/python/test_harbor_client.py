"""Tests for harbor_client: session handling and artifact-scoped deletes."""

from unittest.mock import MagicMock

import pytest
import requests
from harbor_client import (
    PurgeOutcome,
    _NoCookieJar,
    create_harbor_session,
    fetch_csrf_token,
    purge_cached_artifact,
)
from test_pull_failures import VALID_DIGEST


class TestNoCookieJar:
    def test_set_cookie_is_noop(self):
        jar = _NoCookieJar()
        jar.set_cookie(MagicMock())
        assert len(jar) == 0

    def test_extract_cookies_is_noop(self):
        jar = _NoCookieJar()
        jar.extract_cookies(MagicMock(), MagicMock())
        assert len(jar) == 0


class TestCreateHarborSession:
    def test_sets_auth(self):
        session = create_harbor_session("http://harbor:80", "secret")
        assert session.auth == ("admin", "secret")

    def test_sets_headers(self):
        session = create_harbor_session("http://harbor:80", "secret")
        assert session.headers["Content-Type"] == "application/json"

    def test_uses_no_cookie_jar(self):
        session = create_harbor_session("http://harbor:80", "secret")
        assert isinstance(session.cookies, _NoCookieJar)


class TestFetchCsrfToken:
    def test_sets_token_header(self):
        session = MagicMock()
        session.headers = {}
        resp = MagicMock()
        resp.headers = {"X-Harbor-CSRF-Token": "tok123"}
        session.get.return_value = resp
        fetch_csrf_token(session, "http://harbor:80")
        assert session.headers["X-Harbor-CSRF-Token"] == "tok123"

    def test_no_token_in_response(self):
        session = MagicMock()
        session.headers = {}
        resp = MagicMock()
        resp.headers = {}
        session.get.return_value = resp
        fetch_csrf_token(session, "http://harbor:80")
        assert "X-Harbor-CSRF-Token" not in session.headers


def _session(status_code=200, text=""):
    session = MagicMock()
    session.delete.return_value = MagicMock(status_code=status_code, text=text)
    return session


class TestPurgeCachedArtifact:
    def test_success(self):
        session = _session(200)
        outcome = purge_cached_artifact(session, "http://h", "dockerhub-cache", "grafana/alloy", "v1.14.0")
        assert outcome is PurgeOutcome.PURGED

    def test_tag_scoped_url(self):
        session = _session(200)
        purge_cached_artifact(session, "http://h", "dockerhub-cache", "grafana/alloy", "v1.14.0")
        assert session.delete.call_args[0][0] == (
            "http://h/api/v2.0/projects/dockerhub-cache/repositories/grafana%252Falloy/artifacts/v1.14.0"
        )

    def test_digest_reference_is_encoded_once(self):
        session = _session(200)
        purge_cached_artifact(session, "http://h", "ghcr-cache", "actions/runner", VALID_DIGEST)
        encoded = VALID_DIGEST.replace(":", "%3A")
        assert session.delete.call_args[0][0] == (
            f"http://h/api/v2.0/projects/ghcr-cache/repositories/actions%252Frunner/artifacts/{encoded}"
        )

    def test_already_gone_is_absent_not_failed(self):
        session = _session(404)
        outcome = purge_cached_artifact(session, "http://h", "dockerhub-cache", "grafana/alloy", "v1.14.0")
        assert outcome is PurgeOutcome.ABSENT

    def test_referenced_by_index_is_not_failed(self):
        session = _session(412)
        outcome = purge_cached_artifact(session, "http://h", "ghcr-cache", "actions/runner", "v3")
        assert outcome is PurgeOutcome.REFERENCED

    def test_digest_not_found_is_unresolved_not_absent(self):
        session = _session(404)
        outcome = purge_cached_artifact(session, "http://h", "ghcr-cache", "actions/runner", VALID_DIGEST)
        assert outcome is PurgeOutcome.UNRESOLVED

    def test_repo_path_metacharacters_are_encoded(self):
        session = _session(200)
        purge_cached_artifact(session, "http://h", "ghcr-cache", "evil?all=1", "v1")
        url = session.delete.call_args[0][0]
        assert "?" not in url
        assert url.endswith("/repositories/evil%3Fall%3D1/artifacts/v1")

    @pytest.mark.parametrize(
        "repo_path",
        ["grafana/alloy", "actions/runner", "docker/library/nginx", "pause", "a.b_c-d/e"],
    )
    def test_encoding_unchanged_for_valid_repo_paths(self, repo_path):
        session = _session(200)
        purge_cached_artifact(session, "http://h", "ghcr-cache", repo_path, "v1")
        assert f"/repositories/{repo_path.replace('/', '%252F')}/artifacts/v1" in session.delete.call_args[0][0]

    def test_server_error(self):
        session = _session(500, text="internal error")
        outcome = purge_cached_artifact(session, "http://h", "dockerhub-cache", "grafana/alloy", "v1.14.0")
        assert outcome is PurgeOutcome.FAILED

    def test_network_error(self):
        session = MagicMock()
        session.delete.side_effect = requests.ConnectionError("refused")
        outcome = purge_cached_artifact(session, "http://h", "dockerhub-cache", "grafana/alloy", "v1.14.0")
        assert outcome is PurgeOutcome.FAILED

    def test_deep_repo_path_encoding(self):
        session = _session(200)
        purge_cached_artifact(session, "http://h", "ecr-public-cache", "docker/library/nginx", "latest")
        assert "docker%252Flibrary%252Fnginx/artifacts/latest" in session.delete.call_args[0][0]


class TestPurgeOutcome:
    def test_only_failed_is_worth_a_nonzero_exit(self):
        # HarborCacheRecoveryFailing is a critical page keyed on kube_job_status_failed.
        # Every other outcome is a state that no amount of retrying will clear.
        assert set(PurgeOutcome) == {
            PurgeOutcome.PURGED,
            PurgeOutcome.ABSENT,
            PurgeOutcome.REFERENCED,
            PurgeOutcome.UNRESOLVED,
            PurgeOutcome.FAILED,
        }
