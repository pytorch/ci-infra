"""Tests for harbor_cache_recovery module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests
from harbor_cache_recovery import (
    CACHE_CORRUPTION_INDICATORS,
    REGISTRY_TO_PROJECT,
    _extract_waiting_failures,
    _NoCookieJar,
    create_harbor_session,
    fetch_csrf_token,
    find_pull_failures,
    get_config,
    main,
    parse_image_reference,
    purge_cached_repo,
)

# ============================================================================
# parse_image_reference
# ============================================================================


class TestParseImageReference:
    def test_docker_hub_short(self):
        assert parse_image_reference("nginx") == ("docker.io", "library/nginx")

    def test_docker_hub_short_with_tag(self):
        assert parse_image_reference("nginx:latest") == ("docker.io", "library/nginx")

    def test_docker_hub_org(self):
        assert parse_image_reference("grafana/alloy:v1.14.0") == ("docker.io", "grafana/alloy")

    def test_docker_hub_explicit(self):
        assert parse_image_reference("docker.io/grafana/alloy:v1.14.0") == ("docker.io", "grafana/alloy")

    def test_ghcr(self):
        assert parse_image_reference("ghcr.io/actions/runner:latest") == ("ghcr.io", "actions/runner")

    def test_quay(self):
        result = parse_image_reference("quay.io/prometheus-operator/prometheus-config-reloader:v0.81.0")
        assert result == ("quay.io", "prometheus-operator/prometheus-config-reloader")

    def test_k8s_registry(self):
        assert parse_image_reference("registry.k8s.io/pause:3.9") == ("registry.k8s.io", "pause")

    def test_nvcr(self):
        assert parse_image_reference("nvcr.io/nvidia/cuda:12.0") == ("nvcr.io", "nvidia/cuda")

    def test_ecr_public(self):
        result = parse_image_reference("public.ecr.aws/docker/library/nginx:latest")
        assert result == ("public.ecr.aws", "docker/library/nginx")

    def test_digest_reference(self):
        result = parse_image_reference("ghcr.io/actions/runner@sha256:abc123")
        assert result == ("ghcr.io", "actions/runner")

    def test_unknown_registry_returns_none(self):
        assert parse_image_reference("my-private-registry.com/app:v1") is None

    def test_localhost_returns_none(self):
        assert parse_image_reference("localhost:30002/osdc/image:tag") is None

    def test_harbor_hostname_returns_none(self):
        assert parse_image_reference("harbor:30002/osdc/image:tag") is None

    def test_no_tag(self):
        assert parse_image_reference("grafana/alloy") == ("docker.io", "grafana/alloy")

    def test_deep_path(self):
        result = parse_image_reference("ghcr.io/org/sub/image:v1")
        assert result == ("ghcr.io", "org/sub/image")

    def test_docker_hub_library_explicit(self):
        assert parse_image_reference("docker.io/library/nginx:latest") == ("docker.io", "library/nginx")


# ============================================================================
# _extract_waiting_failures
# ============================================================================


def _make_container_status(image="nginx:latest", reason=None, message=None):
    cs = MagicMock()
    cs.image = image
    if reason:
        cs.state.waiting.reason = reason
        cs.state.waiting.message = message or ""
    else:
        cs.state.waiting = None
    return cs


class TestExtractWaitingFailures:
    def test_none_statuses(self):
        assert _extract_waiting_failures(None) == []

    def test_empty_list(self):
        assert _extract_waiting_failures([]) == []

    def test_running_container_skipped(self):
        cs = _make_container_status()
        assert _extract_waiting_failures([cs]) == []

    def test_imagepullbackoff_without_corruption_skipped(self):
        cs = _make_container_status(
            reason="ImagePullBackOff",
            message="unauthorized: authentication required",
        )
        assert _extract_waiting_failures([cs]) == []

    def test_imagepullbackoff_with_size_validation(self):
        msg = "failed size validation: 348055 != 1621: failed precondition"
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message=msg,
        )
        results = _extract_waiting_failures([cs])
        assert len(results) == 1
        assert results[0]["image"] == "grafana/alloy:v1.14.0"

    def test_errimagepull_with_precondition(self):
        cs = _make_container_status(
            image="ghcr.io/actions/runner:latest",
            reason="ErrImagePull",
            message="something failed precondition something",
        )
        results = _extract_waiting_failures([cs])
        assert len(results) == 1

    def test_other_waiting_reasons_skipped(self):
        cs = _make_container_status(reason="CrashLoopBackOff", message="failed size validation")
        assert _extract_waiting_failures([cs]) == []

    def test_unexpected_content_digest(self):
        cs = _make_container_status(
            reason="ImagePullBackOff",
            message="unexpected content digest sha256:abc",
        )
        assert len(_extract_waiting_failures([cs])) == 1


# ============================================================================
# find_pull_failures
# ============================================================================


def _make_pod(
    name="test-pod",
    namespace="default",
    age_seconds=300,
    container_statuses=None,
    init_container_statuses=None,
):
    now = datetime.now(UTC)
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.creationTimestamp = now - timedelta(seconds=age_seconds)
    pod.status.containerStatuses = container_statuses
    pod.status.initContainerStatuses = init_container_statuses
    return pod


class TestFindPullFailures:
    def test_no_pods(self):
        client = MagicMock()
        client.list.return_value = []
        assert find_pull_failures(client, 120) == []

    def test_skips_young_pods(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 348055 != 1621",
        )
        pod = _make_pod(age_seconds=60, container_statuses=[cs])
        client = MagicMock()
        client.list.return_value = [pod]
        assert find_pull_failures(client, 120) == []

    def test_detects_corruption(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 348055 != 1621: failed precondition",
        )
        pod = _make_pod(name="alloy-abc", namespace="logging", container_statuses=[cs])
        client = MagicMock()
        client.list.return_value = [pod]
        results = find_pull_failures(client, 120)
        assert len(results) == 1
        assert results[0]["pod_name"] == "alloy-abc"
        assert results[0]["namespace"] == "logging"
        assert results[0]["harbor_project"] == "dockerhub-cache"
        assert results[0]["repo_path"] == "grafana/alloy"

    def test_skips_auth_errors(self):
        cs = _make_container_status(
            image="ghcr.io/private/repo:v1",
            reason="ImagePullBackOff",
            message="unauthorized: authentication required",
        )
        pod = _make_pod(container_statuses=[cs])
        client = MagicMock()
        client.list.return_value = [pod]
        assert find_pull_failures(client, 120) == []

    def test_skips_unknown_registry(self):
        cs = _make_container_status(
            image="my-registry.com/app:v1",
            reason="ImagePullBackOff",
            message="failed size validation: 100 != 200",
        )
        pod = _make_pod(container_statuses=[cs])
        client = MagicMock()
        client.list.return_value = [pod]
        assert find_pull_failures(client, 120) == []

    def test_detects_init_container_failures(self):
        cs = _make_container_status(
            image="quay.io/prom/node-exporter:v1.7.0",
            reason="ErrImagePull",
            message="failed precondition",
        )
        pod = _make_pod(init_container_statuses=[cs])
        client = MagicMock()
        client.list.return_value = [pod]
        results = find_pull_failures(client, 120)
        assert len(results) == 1
        assert results[0]["harbor_project"] == "quay-cache"

    def test_handles_pod_without_status(self):
        pod = _make_pod()
        pod.status = None
        client = MagicMock()
        client.list.return_value = [pod]
        assert find_pull_failures(client, 120) == []

    def test_handles_pod_without_timestamp(self):
        pod = _make_pod()
        pod.metadata.creationTimestamp = None
        client = MagicMock()
        client.list.return_value = [pod]
        assert find_pull_failures(client, 120) == []

    def test_naive_timestamp_treated_as_utc(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = _make_pod(container_statuses=[cs])
        pod.metadata.creationTimestamp = pod.metadata.creationTimestamp.replace(tzinfo=None)
        client = MagicMock()
        client.list.return_value = [pod]
        assert len(find_pull_failures(client, 120)) == 1


# ============================================================================
# Harbor API functions
# ============================================================================


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


class TestPurgeCachedRepo:
    def test_success(self):
        session = MagicMock()
        session.delete.return_value = MagicMock(status_code=200)
        assert purge_cached_repo(session, "http://h", "dockerhub-cache", "grafana/alloy") is True
        call_url = session.delete.call_args[0][0]
        assert "grafana%252Falloy" in call_url

    def test_already_gone(self):
        session = MagicMock()
        session.delete.return_value = MagicMock(status_code=404)
        assert purge_cached_repo(session, "http://h", "dockerhub-cache", "grafana/alloy") is True

    def test_server_error(self):
        session = MagicMock()
        resp = MagicMock(status_code=500, text="internal error")
        session.delete.return_value = resp
        assert purge_cached_repo(session, "http://h", "dockerhub-cache", "grafana/alloy") is False

    def test_network_error(self):
        session = MagicMock()
        session.delete.side_effect = requests.ConnectionError("refused")
        assert purge_cached_repo(session, "http://h", "dockerhub-cache", "grafana/alloy") is False

    def test_deep_repo_path_encoding(self):
        session = MagicMock()
        session.delete.return_value = MagicMock(status_code=200)
        purge_cached_repo(session, "http://h", "ecr-public-cache", "docker/library/nginx")
        call_url = session.delete.call_args[0][0]
        assert "docker%252Flibrary%252Fnginx" in call_url


# ============================================================================
# get_config
# ============================================================================


class TestGetConfig:
    def test_defaults(self):
        with patch.dict("os.environ", {}, clear=True):
            config = get_config()
        assert config["harbor_url"] == "http://harbor.harbor-system.svc.cluster.local:80"
        assert config["harbor_password"] == ""
        assert config["min_pod_age_seconds"] == 120
        assert config["dry_run"] is False

    def test_custom_values(self):
        env = {
            "HARBOR_URL": "http://custom:8080",
            "HARBOR_ADMIN_PASSWORD": "pw",
            "MIN_POD_AGE_SECONDS": "60",
            "DRY_RUN": "true",
        }
        with patch.dict("os.environ", env, clear=True):
            config = get_config()
        assert config["harbor_url"] == "http://custom:8080"
        assert config["harbor_password"] == "pw"  # noqa: S105
        assert config["min_pod_age_seconds"] == 60
        assert config["dry_run"] is True

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes"])
    def test_dry_run_truthy(self, value):
        with patch.dict("os.environ", {"DRY_RUN": value}, clear=True):
            assert get_config()["dry_run"] is True

    @pytest.mark.parametrize("value", ["false", "0", "no", ""])
    def test_dry_run_falsy(self, value):
        with patch.dict("os.environ", {"DRY_RUN": value}, clear=True):
            assert get_config()["dry_run"] is False


# ============================================================================
# main
# ============================================================================


class TestMain:
    def test_missing_password(self):
        with patch.dict("os.environ", {}, clear=True):
            assert main() == 1

    def test_no_failures_found(self):
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.return_value = []
            assert main() == 0

    def test_dry_run_skips_purge(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = _make_pod(name="alloy-x", namespace="logging", container_statuses=[cs])
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch("harbor_cache_recovery.create_harbor_session") as mock_session,
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw", "DRY_RUN": "true"}, clear=True),
        ):
            mock_cls.return_value.list.return_value = [pod]
            assert main() == 0
            mock_session.assert_not_called()

    def test_purges_on_detection(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 348055 != 1621: failed precondition",
        )
        pod = _make_pod(name="alloy-x", namespace="logging", container_statuses=[cs])
        session = MagicMock()
        session.delete.return_value = MagicMock(status_code=200)
        session.get.return_value = MagicMock(headers={"X-Harbor-CSRF-Token": "tok"})
        session.headers = {}
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch("harbor_cache_recovery.create_harbor_session", return_value=session),
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.return_value = [pod]
            assert main() == 0
            session.delete.assert_called_once()
            assert "grafana%252Falloy" in session.delete.call_args[0][0]

    def test_deduplicates_repos(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pods = [_make_pod(name=f"alloy-{i}", namespace="logging", container_statuses=[cs]) for i in range(3)]
        session = MagicMock()
        session.delete.return_value = MagicMock(status_code=200)
        session.get.return_value = MagicMock(headers={})
        session.headers = {}
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch("harbor_cache_recovery.create_harbor_session", return_value=session),
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.return_value = pods
            assert main() == 0
            assert session.delete.call_count == 1

    def test_harbor_connection_failure(self):
        cs = _make_container_status(
            image="grafana/alloy:v1.14.0",
            reason="ImagePullBackOff",
            message="failed size validation: 1 != 2",
        )
        pod = _make_pod(container_statuses=[cs])
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("refused")
        session.headers = {}
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch("harbor_cache_recovery.create_harbor_session", return_value=session),
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.return_value = [pod]
            assert main() == 1

    def test_partial_purge_failure(self):
        images = [
            ("grafana/alloy:v1.14.0", "failed size validation: 1 != 2"),
            ("ghcr.io/actions/runner:v3", "failed precondition"),
        ]
        pods = []
        for img, msg in images:
            cs = _make_container_status(image=img, reason="ImagePullBackOff", message=msg)
            pods.append(_make_pod(name=f"pod-{img}", container_statuses=[cs]))
        session = MagicMock()
        session.delete.side_effect = [MagicMock(status_code=200), MagicMock(status_code=500, text="err")]
        session.get.return_value = MagicMock(headers={})
        session.headers = {}
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch("harbor_cache_recovery.create_harbor_session", return_value=session),
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.return_value = pods
            assert main() == 1

    def test_pod_scan_failure(self):
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.side_effect = Exception("API error")
            assert main() == 1


# ============================================================================
# Constants
# ============================================================================


class TestConstants:
    def test_all_registries_covered(self):
        expected = {"docker.io", "ghcr.io", "public.ecr.aws", "nvcr.io", "registry.k8s.io", "quay.io"}
        assert set(REGISTRY_TO_PROJECT.keys()) == expected

    def test_corruption_indicators_non_empty(self):
        assert len(CACHE_CORRUPTION_INDICATORS) >= 4
