"""Unit tests for handle_get_logs — the Function URL handler powering
`gpu-dev debug --logs` (CloudWatch Logs Insights query for one reservation)."""
import json
from unittest.mock import MagicMock


def test_get_logs_requires_fields(lambda_index):
    r = lambda_index.handle_get_logs({"reservation_id": "", "user_id": ""})
    assert r["statusCode"] == 400


def test_get_logs_ownership_enforced(lambda_index, monkeypatch):
    # find_reservation_by_prefix(user_id=...) raises for someone else's reservation.
    def _raise(rid, user_id=None):
        raise ValueError(f"Reservation {rid} not found for user {user_id}")
    monkeypatch.setattr(lambda_index, "find_reservation_by_prefix", _raise)
    r = lambda_index.handle_get_logs({"reservation_id": "abc", "user_id": "x@y.com"})
    assert r["statusCode"] == 404
    assert "not found" in json.loads(r["body"])["error"]


def test_get_logs_happy_path(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "find_reservation_by_prefix",
                        lambda rid, user_id=None: {
                            "reservation_id": "9b1466cc-f272-40a6-90da-2bf0f4c1e599",
                            "created_at": "2026-06-09T19:51:00"})
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q1"}
    logs.get_query_results.return_value = {"status": "Complete", "results": [
        [{"field": "@timestamp", "value": "2026-06-09 20:07:30.123"},
         {"field": "@message", "value": "Creating pod gpu-dev-9b1466cc\n"}],
    ]}
    monkeypatch.setattr(lambda_index.boto3, "client", lambda svc, *a, **k: logs)

    r = lambda_index.handle_get_logs({"reservation_id": "9b1466cc", "user_id": "x@y.com"})
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert len(body["lines"]) == 1
    assert body["lines"][0]["message"] == "Creating pod gpu-dev-9b1466cc"  # newline stripped
    # query filters on the 8-char id prefix, scoped to the reservation's lifetime
    kw = logs.start_query.call_args.kwargs
    assert "9b1466cc" in kw["queryString"]
    assert kw["startTime"] < kw["endTime"]


def test_get_logs_query_incomplete_returns_error(lambda_index, monkeypatch):
    monkeypatch.setattr(lambda_index, "find_reservation_by_prefix",
                        lambda rid, user_id=None: {"reservation_id": "9b1466cc-x",
                                                   "created_at": "2026-06-09T19:51:00"})
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q1"}
    logs.get_query_results.return_value = {"status": "Failed", "results": []}
    monkeypatch.setattr(lambda_index.boto3, "client", lambda svc, *a, **k: logs)
    monkeypatch.setattr(lambda_index.time, "sleep", lambda *_a, **_k: None)

    r = lambda_index.handle_get_logs({"reservation_id": "9b1466cc", "user_id": "x@y.com"})
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["lines"] == [] and "did not complete" in body["error"]
