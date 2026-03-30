"""Tests for configure_harbor_projects.py."""

import argparse
from unittest.mock import MagicMock, patch

import pytest
import requests
from configure_harbor_projects import (
    REGISTRIES,
    _endpoint_has_credentials,
    create_proxy_cache_project,
    create_session,
    delete_project,
    delete_registry_endpoint,
    ensure_registry_endpoint,
    fetch_csrf_token,
    get_registry_info,
    main,
    wait_for_harbor,
)

HARBOR_URL = "http://harbor.test:30002"


def make_response(status_code, json_data=None, text=""):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# _endpoint_has_credentials
# ---------------------------------------------------------------------------


class TestEndpointHasCredentials:
    def test_has_credentials_with_type(self):
        info = {"credential": {"type": "basic", "access_key": "u"}}
        assert _endpoint_has_credentials(info) is True

    def test_no_credential_key(self):
        info = {"name": "test"}
        assert _endpoint_has_credentials(info) is False

    def test_empty_credential_dict(self):
        info = {"credential": {}}
        assert _endpoint_has_credentials(info) is False

    def test_credential_with_no_type(self):
        info = {"credential": {"access_key": "u"}}
        assert _endpoint_has_credentials(info) is False

    def test_none_credential(self):
        info = {"credential": None}
        assert _endpoint_has_credentials(info) is False


# ---------------------------------------------------------------------------
# ensure_registry_endpoint
# ---------------------------------------------------------------------------


SAMPLE_REGISTRY = {
    "name": "dockerhub",
    "url": "https://hub.docker.com",
    "type": "docker-hub",
    "project_name": "dockerhub-cache",
}


class TestEnsureRegistryEndpoint:
    def test_create_new_201(self):
        session = MagicMock()
        # get_registry_info: GET returns empty list (no existing)
        session.get.return_value = make_response(200, [])
        # POST returns 201
        session.post.return_value = make_response(201)

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is True
        session.post.assert_called_once()
        post_kwargs = session.post.call_args
        assert post_kwargs[0][0] == f"{HARBOR_URL}/api/v2.0/registries"

    def test_create_new_conflict_409(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [])
        session.post.return_value = make_response(409)

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is True

    def test_already_exists_by_get(self):
        session = MagicMock()
        existing = {"id": 5, "name": "dockerhub", "credential": {"type": "basic"}}
        session.get.return_value = make_response(200, [existing])

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is True
        # Should NOT post — already exists
        session.post.assert_not_called()

    def test_recreate_with_credentials(self):
        """Endpoint exists without creds, new creds provided -> delete + recreate."""
        session = MagicMock()
        existing_no_creds = {"id": 7, "name": "dockerhub", "credential": {}}
        credentials = {"type": "basic", "access_key": "u", "access_secret": "p"}

        # Call sequence:
        # 1. get_registry_info GET -> returns existing (no creds)
        # 2. delete_project: GET repos page 1 -> empty
        # 3. delete_project: DELETE project -> 200
        # 4. delete_registry_endpoint: DELETE registry -> 200
        # 5. POST new registry -> 201
        session.get.side_effect = [
            make_response(200, [existing_no_creds]),  # get_registry_info
            make_response(200, []),  # delete_project: list repos (empty)
        ]
        session.delete.side_effect = [
            make_response(200),  # delete project
            make_response(200),  # delete registry endpoint
        ]
        session.post.return_value = make_response(201)

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY, credentials=credentials)

        assert result is True
        # Verify the POST payload includes credentials
        post_call = session.post.call_args
        assert post_call[1]["json"]["credential"] == credentials

    def test_create_fails_500(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [])
        session.post.return_value = make_response(500, text="Internal Server Error")

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is False

    def test_recreate_delete_project_fails(self):
        """Recreate path: delete_project fails -> return False (line 258)."""
        session = MagicMock()
        existing_no_creds = {"id": 7, "name": "dockerhub", "credential": {}}
        credentials = {"type": "basic", "access_key": "u", "access_secret": "p"}

        # get_registry_info returns existing (no creds)
        session.get.side_effect = [
            make_response(200, [existing_no_creds]),  # get_registry_info
            make_response(200, []),  # delete_project: list repos
        ]
        # delete_project's DELETE returns 500 -> delete_project returns False
        session.delete.return_value = make_response(500, text="error")

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY, credentials=credentials)

        assert result is False

    def test_recreate_delete_endpoint_fails(self):
        """Recreate path: delete_registry_endpoint fails -> return False (line 260)."""
        session = MagicMock()
        existing_no_creds = {"id": 7, "name": "dockerhub", "credential": {}}
        credentials = {"type": "basic", "access_key": "u", "access_secret": "p"}

        session.get.side_effect = [
            make_response(200, [existing_no_creds]),  # get_registry_info
            make_response(200, []),  # delete_project: list repos
        ]
        session.delete.side_effect = [
            make_response(200),  # delete_project succeeds
            make_response(500, text="error"),  # delete_registry_endpoint fails
        ]

        result = ensure_registry_endpoint(session, HARBOR_URL, SAMPLE_REGISTRY, credentials=credentials)

        assert result is False


# ---------------------------------------------------------------------------
# create_proxy_cache_project
# ---------------------------------------------------------------------------


class TestCreateProxyCacheProject:
    def test_success_201(self):
        session = MagicMock()
        # get_registry_info returns the endpoint
        session.get.return_value = make_response(200, [{"id": 3, "name": "dockerhub"}])
        session.post.return_value = make_response(201)

        result = create_proxy_cache_project(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is True
        post_kwargs = session.post.call_args[1]
        assert post_kwargs["json"]["project_name"] == "dockerhub-cache"
        assert post_kwargs["json"]["registry_id"] == 3

    def test_conflict_409(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [{"id": 3, "name": "dockerhub"}])
        session.post.return_value = make_response(409)

        result = create_proxy_cache_project(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is True

    def test_registry_not_found(self):
        session = MagicMock()
        # get_registry_info returns empty list -> None
        session.get.return_value = make_response(200, [])

        result = create_proxy_cache_project(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is False
        session.post.assert_not_called()

    def test_create_fails_500(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [{"id": 3, "name": "dockerhub"}])
        session.post.return_value = make_response(500, text="error")

        result = create_proxy_cache_project(session, HARBOR_URL, SAMPLE_REGISTRY)

        assert result is False


# ---------------------------------------------------------------------------
# delete_project
# ---------------------------------------------------------------------------


class TestDeleteProject:
    def test_empty_project(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [])  # no repos
        session.delete.return_value = make_response(200)  # delete project

        result = delete_project(session, HARBOR_URL, "dockerhub-cache")

        assert result is True
        # One DELETE for the project itself
        assert session.delete.call_count == 1

    def test_project_with_repos(self):
        session = MagicMock()
        repos = [
            {"name": "dockerhub-cache/library/nginx"},
            {"name": "dockerhub-cache/alpine"},
        ]
        session.get.side_effect = [
            make_response(200, repos),  # page 1: two repos
            make_response(200, []),  # page 2: empty
        ]
        session.delete.side_effect = [
            make_response(200),  # delete repo 1
            make_response(200),  # delete repo 2
            make_response(200),  # delete project
        ]

        result = delete_project(session, HARBOR_URL, "dockerhub-cache")

        assert result is True
        # 2 repo deletes + 1 project delete
        assert session.delete.call_count == 3

        # Verify double-encoded slashes in repo paths
        repo_delete_urls = [c[0][0] for c in session.delete.call_args_list[:2]]
        assert any("library%252Fnginx" in url for url in repo_delete_urls)
        assert any("/alpine" in url for url in repo_delete_urls)

    def test_already_gone_404(self):
        session = MagicMock()
        session.get.return_value = make_response(404)  # project doesn't exist
        session.delete.return_value = make_response(404)  # delete returns 404

        result = delete_project(session, HARBOR_URL, "dockerhub-cache")

        assert result is True

    def test_pagination(self):
        """Repos span multiple pages."""
        session = MagicMock()
        page1_repos = [{"name": f"proj/img{i}"} for i in range(100)]
        page2_repos = [{"name": "proj/img100"}]
        session.get.side_effect = [
            make_response(200, page1_repos),  # page 1
            make_response(200, page2_repos),  # page 2
            make_response(200, []),  # page 3: empty
        ]
        # 101 repo deletes + 1 project delete
        session.delete.return_value = make_response(200)

        result = delete_project(session, HARBOR_URL, "proj")

        assert result is True
        assert session.delete.call_count == 102  # 101 repos + 1 project

    def test_delete_project_fails(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [])
        session.delete.return_value = make_response(500, text="error")

        result = delete_project(session, HARBOR_URL, "dockerhub-cache")

        assert result is False

    def test_repo_listing_non_200_breaks(self):
        """Non-200, non-404 status on repo listing breaks the loop (line 201)."""
        session = MagicMock()
        session.get.return_value = make_response(500)  # repo listing fails
        session.delete.return_value = make_response(200)  # project delete succeeds

        result = delete_project(session, HARBOR_URL, "dockerhub-cache")

        assert result is True
        # Should still attempt to delete the project itself
        session.delete.assert_called_once()

    def test_repo_delete_failure_warns(self, capsys):
        """Repo delete failure logs a warning but continues (line 218)."""
        session = MagicMock()
        repos = [{"name": "proj/img1"}]
        session.get.side_effect = [
            make_response(200, repos),  # page 1: one repo
            make_response(200, []),  # page 2: empty
        ]
        session.delete.side_effect = [
            make_response(500, text="error"),  # repo delete fails
            make_response(200),  # project delete succeeds
        ]

        result = delete_project(session, HARBOR_URL, "proj")

        assert result is True
        assert session.delete.call_count == 2


# ---------------------------------------------------------------------------
# wait_for_harbor
# ---------------------------------------------------------------------------


class TestWaitForHarbor:
    @patch("configure_harbor_projects.time")
    def test_ready_immediately(self, mock_time):
        mock_time.time.return_value = 0
        session = MagicMock()
        session.get.return_value = make_response(200)

        result = wait_for_harbor(session, HARBOR_URL, timeout=300)

        assert result is True
        mock_time.sleep.assert_not_called()

    @patch("configure_harbor_projects.time")
    def test_connection_error_then_ready(self, mock_time):
        # First call at t=0, second at t=5, third at t=10
        mock_time.time.side_effect = [0, 5, 10]
        session = MagicMock()
        session.get.side_effect = [
            requests.ConnectionError("refused"),
            make_response(200),
        ]

        result = wait_for_harbor(session, HARBOR_URL, timeout=300)

        assert result is True
        mock_time.sleep.assert_called_once_with(5)

    @patch("configure_harbor_projects.time")
    def test_timeout(self, mock_time):
        # Simulate time progressing past timeout
        mock_time.time.side_effect = [0, 100, 200, 301]
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("refused")

        result = wait_for_harbor(session, HARBOR_URL, timeout=300)

        assert result is False

    @patch("configure_harbor_projects.time")
    def test_non_200_then_ready(self, mock_time):
        mock_time.time.side_effect = [0, 5, 10]
        session = MagicMock()
        session.get.side_effect = [
            make_response(503),
            make_response(200),
        ]

        result = wait_for_harbor(session, HARBOR_URL, timeout=300)

        assert result is True
        mock_time.sleep.assert_called_once_with(5)


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_basic_auth(self):
        session = create_session(HARBOR_URL, "s3cret")
        assert session.auth == ("admin", "s3cret")

    def test_headers(self):
        session = create_session(HARBOR_URL, "pw")
        assert session.headers["Content-Type"] == "application/json"
        assert session.headers["Accept"] == "application/json"

    def test_retry_adapter_mounted(self):
        session = create_session(HARBOR_URL, "pw")
        # Both http and https should have adapters with retry config
        http_adapter = session.get_adapter("http://example.com")
        https_adapter = session.get_adapter("https://example.com")
        assert http_adapter.max_retries.total == 3
        assert http_adapter.max_retries.backoff_factor == 1
        assert 502 in http_adapter.max_retries.status_forcelist
        assert 503 in http_adapter.max_retries.status_forcelist
        assert 504 in http_adapter.max_retries.status_forcelist
        assert https_adapter.max_retries.total == 3


# ---------------------------------------------------------------------------
# fetch_csrf_token
# ---------------------------------------------------------------------------


class TestFetchCsrfToken:
    def test_token_present(self):
        session = MagicMock()
        session.headers = {}
        resp = make_response(200)
        resp.headers = {"X-Harbor-CSRF-Token": "tok123"}
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp

        fetch_csrf_token(session, HARBOR_URL)

        session.get.assert_called_once_with(f"{HARBOR_URL}/api/v2.0/systeminfo", timeout=10)
        assert session.headers["X-Harbor-CSRF-Token"] == "tok123"

    def test_token_absent(self):
        session = MagicMock()
        session.headers = {}
        resp = make_response(200)
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        session.get.return_value = resp

        fetch_csrf_token(session, HARBOR_URL)

        assert "X-Harbor-CSRF-Token" not in session.headers

    def test_request_failure_raises(self):
        session = MagicMock()
        session.headers = {}
        resp = make_response(500)
        resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
        session.get.return_value = resp

        with pytest.raises(requests.HTTPError):
            fetch_csrf_token(session, HARBOR_URL)


# ---------------------------------------------------------------------------
# get_registry_info
# ---------------------------------------------------------------------------


class TestGetRegistryInfo:
    def test_name_match(self):
        session = MagicMock()
        registries = [
            {"name": "other", "id": 1},
            {"name": "dockerhub", "id": 2},
        ]
        session.get.return_value = make_response(200, registries)

        result = get_registry_info(session, HARBOR_URL, "dockerhub")

        assert result == {"name": "dockerhub", "id": 2}
        session.get.assert_called_once_with(
            f"{HARBOR_URL}/api/v2.0/registries",
            params={"name": "dockerhub"},
            timeout=30,
        )

    def test_no_match(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [{"name": "other", "id": 1}])

        result = get_registry_info(session, HARBOR_URL, "dockerhub")

        assert result is None

    def test_empty_list(self):
        session = MagicMock()
        session.get.return_value = make_response(200, [])

        result = get_registry_info(session, HARBOR_URL, "dockerhub")

        assert result is None

    def test_non_200(self):
        session = MagicMock()
        session.get.return_value = make_response(500)

        result = get_registry_info(session, HARBOR_URL, "dockerhub")

        assert result is None


# ---------------------------------------------------------------------------
# delete_registry_endpoint
# ---------------------------------------------------------------------------


class TestDeleteRegistryEndpoint:
    def test_success(self):
        session = MagicMock()
        session.delete.return_value = make_response(200)

        result = delete_registry_endpoint(session, HARBOR_URL, 7, "dockerhub")

        assert result is True
        session.delete.assert_called_once_with(
            f"{HARBOR_URL}/api/v2.0/registries/7",
            timeout=30,
        )

    def test_failure(self):
        session = MagicMock()
        session.delete.return_value = make_response(500, text="Internal Server Error")

        result = delete_registry_endpoint(session, HARBOR_URL, 7, "dockerhub")

        assert result is False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    @patch("configure_harbor_projects.sys")
    def test_missing_admin_password(self, mock_sys):
        """main() should fail when --admin-password is not provided."""
        mock_sys.argv = ["configure_harbor_projects.py"]
        # argparse calls sys.exit on error; also parser.error calls sys.exit
        mock_sys.exit.side_effect = SystemExit(2)

        with pytest.raises(SystemExit):
            main()

    @patch("configure_harbor_projects.create_proxy_cache_project", return_value=True)
    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=True)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=True)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_end_to_end_success(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
        mock_create_proj,
    ):
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username=None,
            dockerhub_token=None,
            github_username=None,
            github_token=None,
            no_wait=False,
        )
        mock_create_session.return_value = MagicMock()

        result = main()

        assert result == 0
        mock_create_session.assert_called_once_with(HARBOR_URL, "pw")
        mock_wait.assert_called_once()
        mock_csrf.assert_called_once()
        assert mock_ensure.call_count == len(REGISTRIES)
        assert mock_create_proj.call_count == len(REGISTRIES)

    @patch("configure_harbor_projects.create_proxy_cache_project", return_value=True)
    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=True)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=True)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_no_wait_skips_harbor_wait(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
        mock_create_proj,
    ):
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username=None,
            dockerhub_token=None,
            github_username=None,
            github_token=None,
            no_wait=True,
        )
        mock_create_session.return_value = MagicMock()

        result = main()

        assert result == 0
        mock_wait.assert_not_called()

    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=False)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=True)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_returns_1_on_endpoint_failure(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
    ):
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username=None,
            dockerhub_token=None,
            github_username=None,
            github_token=None,
            no_wait=True,
        )
        mock_create_session.return_value = MagicMock()

        result = main()

        assert result == 1

    @patch("configure_harbor_projects.create_proxy_cache_project")
    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=True)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=True)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_create_proxy_cache_failure_returns_1(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
        mock_create_proj,
    ):
        """main() returns 1 when create_proxy_cache_project fails (line 395)."""
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username=None,
            dockerhub_token=None,
            github_username=None,
            github_token=None,
            no_wait=True,
        )
        mock_create_session.return_value = MagicMock()
        mock_create_proj.return_value = False

        result = main()

        assert result == 1

    @patch("configure_harbor_projects.create_proxy_cache_project", return_value=True)
    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=True)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=False)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_wait_for_harbor_failure_returns_1(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
        mock_create_proj,
    ):
        """main() returns 1 when wait_for_harbor returns False (line 380)."""
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username=None,
            dockerhub_token=None,
            github_username=None,
            github_token=None,
            no_wait=False,
        )
        mock_create_session.return_value = MagicMock()

        result = main()

        assert result == 1
        mock_wait.assert_called_once()
        # Should not proceed to endpoints or projects
        mock_ensure.assert_not_called()

    @patch("configure_harbor_projects.create_proxy_cache_project", return_value=True)
    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=True)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=True)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_github_credentials_passed(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
        mock_create_proj,
    ):
        """main() passes GitHub credentials when provided (lines 369-374)."""
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username=None,
            dockerhub_token=None,
            github_username="ghuser",
            github_token="ghtoken",
            no_wait=True,
        )
        mock_create_session.return_value = MagicMock()

        result = main()

        assert result == 0
        # Find the call for the ghcr registry
        for call in mock_ensure.call_args_list:
            _session, _url, reg = call[0][:3]
            creds = call[0][3] if len(call[0]) > 3 else call[1].get("credentials")
            if reg["name"] == "ghcr":
                assert creds == {
                    "type": "basic",
                    "access_key": "ghuser",
                    "access_secret": "ghtoken",
                }
                break
        else:
            pytest.fail("ghcr registry call not found")

    @patch("configure_harbor_projects.create_proxy_cache_project", return_value=True)
    @patch("configure_harbor_projects.ensure_registry_endpoint", return_value=True)
    @patch("configure_harbor_projects.fetch_csrf_token")
    @patch("configure_harbor_projects.wait_for_harbor", return_value=True)
    @patch("configure_harbor_projects.create_session")
    @patch("configure_harbor_projects.argparse.ArgumentParser.parse_args")
    def test_dockerhub_credentials_passed(
        self,
        mock_parse_args,
        mock_create_session,
        mock_wait,
        mock_csrf,
        mock_ensure,
        mock_create_proj,
    ):
        mock_parse_args.return_value = argparse.Namespace(
            harbor_url=HARBOR_URL,
            admin_password="pw",
            dockerhub_username="user",
            dockerhub_token="tok",
            github_username=None,
            github_token=None,
            no_wait=True,
        )
        mock_create_session.return_value = MagicMock()

        result = main()

        assert result == 0
        # Find the call for the dockerhub registry
        for call in mock_ensure.call_args_list:
            _session, _url, reg = call[0][:3]
            creds = call[0][3] if len(call[0]) > 3 else call[1].get("credentials")
            if reg["name"] == "dockerhub":
                assert creds == {
                    "type": "basic",
                    "access_key": "user",
                    "access_secret": "tok",
                }
                break
        else:
            pytest.fail("dockerhub registry call not found")
