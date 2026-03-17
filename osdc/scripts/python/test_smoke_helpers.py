"""Unit tests for smoke test helper functions (URL derivation)."""

import sys
from pathlib import Path

# helpers.py lives in tests/smoke/ — add it to sys.path for import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tests" / "smoke"))

from helpers import loki_read_url, mimir_read_url


class TestMimirReadUrl:
    def test_standard_url(self):
        assert (
            mimir_read_url("https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/push")
            == "https://prometheus-prod-36-prod-us-west-0.grafana.net/api/prom/api/v1/query"
        )

    def test_trailing_slash(self):
        assert (
            mimir_read_url("https://host.grafana.net/api/prom/push/")
            == "https://host.grafana.net/api/prom/api/v1/query"
        )

    def test_no_push_suffix(self):
        assert mimir_read_url("https://host.grafana.net/api/prom") == "https://host.grafana.net/api/prom/api/v1/query"


class TestLokiReadUrl:
    def test_standard_url(self):
        assert (
            loki_read_url("https://logs-prod-us-central1.grafana.net/loki/api/v1/push")
            == "https://logs-prod-us-central1.grafana.net/loki/api/v1/query_range"
        )

    def test_trailing_slash(self):
        assert (
            loki_read_url("https://host.grafana.net/loki/api/v1/push/")
            == "https://host.grafana.net/loki/api/v1/query_range"
        )

    def test_no_push_suffix(self):
        assert (
            loki_read_url("https://host.grafana.net/loki/api/v1") == "https://host.grafana.net/loki/api/v1/query_range"
        )
