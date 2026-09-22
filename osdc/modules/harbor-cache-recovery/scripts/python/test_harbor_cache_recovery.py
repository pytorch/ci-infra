"""Tests for harbor_cache_recovery: configuration and the main orchestration loop."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from harbor_cache_recovery import MAX_PURGES_PER_RUN, get_config, main
from pull_failures import CACHE_CORRUPTION_INDICATORS
from test_pull_failures import (
    CORRUPTION_WITH_PRECONDITION,
    DISK_FULL,
    HTML_MEDIA_TYPE,
    LAYER_ENOSPC,
    PINNED_RUNNER_IMAGE,
    VALID_DIGEST,
    failing_pod,
)

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


def _run_main(pods, session=None, env=None):
    """Drive main() over a fixed pod list with a mocked Harbor session."""
    environ = {"HARBOR_ADMIN_PASSWORD": "pw"}
    environ.update(env or {})
    with (
        patch("harbor_cache_recovery.Client") as mock_cls,
        patch("harbor_cache_recovery.create_harbor_session", return_value=session or MagicMock()) as mock_factory,
        patch.dict("os.environ", environ, clear=True),
    ):
        mock_cls.return_value.list.return_value = pods
        return main(), mock_factory


def _harbor_session(delete_result):
    session = MagicMock()
    if isinstance(delete_result, list):
        session.delete.side_effect = delete_result
    else:
        session.delete.return_value = delete_result
    session.get.return_value = MagicMock(headers={"X-Harbor-CSRF-Token": "tok"})
    session.headers = {}
    return session


class TestMain:
    def test_missing_password(self):
        with patch.dict("os.environ", {}, clear=True):
            assert main() == 1

    def test_no_failures_found(self):
        assert _run_main([])[0] == 0

    def test_dry_run_skips_purge(self):
        pod = failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION)
        exit_code, factory = _run_main([pod], env={"DRY_RUN": "true"})
        assert exit_code == 0
        factory.assert_not_called()

    @pytest.mark.parametrize("indicator", CACHE_CORRUPTION_INDICATORS)
    def test_every_corruption_indicator_purges(self, indicator):
        message = f'failed to pull and unpack image "docker.io/grafana/alloy:v1.14.0": {indicator} foo'
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main([failing_pod("grafana/alloy:v1.14.0", message)], session=session)
        assert exit_code == 0
        session.delete.assert_called_once()
        assert session.delete.call_args[0][0].endswith("/repositories/grafana%252Falloy/artifacts/v1.14.0")

    def test_html_media_type_never_purged(self):
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, factory = _run_main([failing_pod("rclone/rclone:1.69.1", HTML_MEDIA_TYPE)], session=session)
        assert exit_code == 0
        session.delete.assert_not_called()
        factory.assert_not_called()

    def test_deny_list_wins_over_corruption(self):
        message = f"{CORRUPTION_WITH_PRECONDITION} and also unexpected media type text/html"
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main([failing_pod("grafana/alloy:v1.14.0", message)], session=session)
        assert exit_code == 0
        session.delete.assert_not_called()

    @pytest.mark.parametrize("deny_first", [True, False], ids=["deny_first", "allow_first"])
    def test_same_artifact_denied_on_another_pod_is_never_purged(self, deny_first):
        # Two pods, same image, one deny-shaped message and one corruption-shaped one.
        denied = failing_pod("rclone/rclone:1.69.1", HTML_MEDIA_TYPE, name="rclone-denied")
        allowed = failing_pod("rclone/rclone:1.69.1", CORRUPTION_WITH_PRECONDITION, name="rclone-allowed")
        pods = [denied, allowed] if deny_first else [allowed, denied]
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main(pods, session=session)
        assert exit_code == 0
        session.delete.assert_not_called()

    def test_purgeable_alongside_never_purge(self):
        pods = [
            failing_pod("rclone/rclone:1.69.1", HTML_MEDIA_TYPE, name="rclone-1"),
            failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION, name="alloy-1"),
        ]
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main(pods, session=session)
        assert exit_code == 0
        session.delete.assert_called_once()
        assert "grafana%252Falloy" in session.delete.call_args[0][0]

    def test_deduplicates_identical_artifacts(self):
        pods = [failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION, name=f"alloy-{i}") for i in range(3)]
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main(pods, session=session)
        assert exit_code == 0
        assert session.delete.call_count == 1

    def test_distinct_tags_purged_separately(self):
        pods = [
            failing_pod("grafana/alloy:v1.14.0", "failed size validation: 1 != 2", name="alloy-a"),
            failing_pod("grafana/alloy:v1.13.0", "failed size validation: 1 != 2", name="alloy-b"),
        ]
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main(pods, session=session)
        assert exit_code == 0
        assert session.delete.call_count == 2

    def test_referenced_artifact_does_not_fail_the_job(self):
        session = _harbor_session(MagicMock(status_code=412))
        pod = failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION)
        assert _run_main([pod], session=session)[0] == 0

    def test_digest_only_miss_does_not_fail_the_job(self):
        pod = failing_pod(f"ghcr.io/actions/runner@{VALID_DIGEST}", CORRUPTION_WITH_PRECONDITION)
        session = _harbor_session(MagicMock(status_code=404))
        exit_code, _ = _run_main([pod], session=session)
        assert exit_code == 0
        assert session.delete.call_args[0][0].endswith("/artifacts/" + VALID_DIGEST.replace(":", "%3A"))

    def test_pinned_tag_and_digest_purges_by_tag(self):
        session = _harbor_session(MagicMock(status_code=200))
        pod = failing_pod(PINNED_RUNNER_IMAGE, CORRUPTION_WITH_PRECONDITION)
        exit_code, _ = _run_main([pod], session=session)
        assert exit_code == 0
        assert session.delete.call_args[0][0].endswith("/repositories/actions%252Factions-runner/artifacts/2.336.0")

    @pytest.mark.parametrize("message", [DISK_FULL, LAYER_ENOSPC], ids=["copy_path", "unpack_path"])
    def test_disk_full_never_purges(self, message):
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, factory = _run_main([failing_pod("grafana/alloy:v1.14.0", message)], session=session)
        assert exit_code == 0
        session.delete.assert_not_called()
        factory.assert_not_called()

    def test_absent_artifact_does_not_fail_the_job(self):
        session = _harbor_session(MagicMock(status_code=404))
        pod = failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION)
        assert _run_main([pod], session=session)[0] == 0

    def test_server_error_fails_the_job(self):
        session = _harbor_session(MagicMock(status_code=500, text="err"))
        pod = failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION)
        assert _run_main([pod], session=session)[0] == 1

    def test_absent_mixed_with_purged_stays_green(self):
        pods = [
            failing_pod("grafana/alloy:v1.14.0", "failed size validation: 1 != 2", name="alloy-a"),
            failing_pod("ghcr.io/actions/runner:v3", "unexpected commit size 1, expected 2", name="runner-a"),
        ]
        session = _harbor_session([MagicMock(status_code=200), MagicMock(status_code=404)])
        assert _run_main(pods, session=session)[0] == 0

    def test_partial_purge_failure(self):
        pods = [
            failing_pod("grafana/alloy:v1.14.0", "failed size validation: 1 != 2", name="alloy-a"),
            failing_pod("ghcr.io/actions/runner:v3", "short read: expected 512 bytes", name="runner-a"),
        ]
        session = _harbor_session([MagicMock(status_code=200), MagicMock(status_code=500, text="err")])
        assert _run_main(pods, session=session)[0] == 1

    def test_harbor_connection_failure(self):
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("refused")
        session.headers = {}
        pod = failing_pod("grafana/alloy:v1.14.0", CORRUPTION_WITH_PRECONDITION)
        assert _run_main([pod], session=session)[0] == 1

    def test_caps_purges_per_run(self):
        pods = [
            failing_pod(f"grafana/alloy:v1.14.{i}", "failed size validation: 1 != 2", name=f"alloy-{i}")
            for i in range(MAX_PURGES_PER_RUN + 5)
        ]
        session = _harbor_session(MagicMock(status_code=200))
        exit_code, _ = _run_main(pods, session=session)
        assert exit_code == 0
        assert session.delete.call_count == MAX_PURGES_PER_RUN

    def test_under_the_cap_purges_everything(self):
        pods = [
            failing_pod(f"grafana/alloy:v1.14.{i}", "failed size validation: 1 != 2", name=f"alloy-{i}")
            for i in range(3)
        ]
        session = _harbor_session(MagicMock(status_code=200))
        _run_main(pods, session=session)
        assert session.delete.call_count == 3

    def test_pod_scan_failure(self):
        with (
            patch("harbor_cache_recovery.Client") as mock_cls,
            patch.dict("os.environ", {"HARBOR_ADMIN_PASSWORD": "pw"}, clear=True),
        ):
            mock_cls.return_value.list.side_effect = Exception("API error")
            assert main() == 1
