import base64
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pytest

from test_admin_server import login, request, run_server, session_browser


def test_evidence_serves_unicode_filename_without_breaking_response_headers(tmp_path):
    store, server, base = run_server(tmp_path)
    original = tmp_path / "面单 100%.png"
    original.write_bytes(b"\x89PNG\r\n\x1a\nimage-fixture")
    store.enqueue_inbox("chinese-label", {"path": str(original)})
    authorization = base64.b64encode(b"admin:secret-token").decode()
    try:
        with urlopen(Request(base + "/evidence/chinese-label",
                             headers={"Authorization": "Basic " + authorization})) as response:
            assert response.status == 200
            assert response.read() == original.read_bytes()
            assert response.headers.get_content_type() == "image/png"
            disposition = response.headers["Content-Disposition"]
            assert disposition.isascii()
            assert "filename*=UTF-8''" + quote(original.name, safe="") in disposition
    finally:
        server.shutdown()


@pytest.mark.parametrize("endpoint", ["login", "logout", "api"])
def test_non_ascii_csrf_is_rejected_without_disconnect_or_mutation(tmp_path, endpoint):
    store, server, base = run_server(tmp_path)
    task_id = store.enqueue_task("review", "review:XSD1", {"order": "XSD1"})
    browser, _ = session_browser()
    try:
        if endpoint == "api":
            with pytest.raises(HTTPError) as denied:
                request(base + f"/api/tasks/{task_id}/resolve", "secret-token", "POST",
                        {"reason": "must not execute"}, csrf="é")
        else:
            if endpoint == "logout":
                login(browser, base, "admin", "secret-token").read()
            else:
                browser.open(base + "/login").read()
            data = urlencode({"csrf": "错误令牌", "username": "admin", "password": "secret-token"}).encode()
            with pytest.raises(HTTPError) as denied:
                browser.open(Request(base + "/" + endpoint, data=data, method="POST"))
        assert denied.value.code == 403
        assert store.list_tasks()[0]["status"] == "pending"
        assert not store.list_audit()
        if endpoint == "logout":
            assert browser.open(base + "/orders").geturl() == base + "/orders"
    finally:
        server.shutdown()


@pytest.mark.parametrize("second_observation,expected", [(None, "N/A"), ("invalid", "N/A"),
                                                         ("naive", "N/A"),
                                                         ("fresh", False), ("stale", True)])
def test_shipments_api_does_not_report_partially_unknown_freshness_as_fresh(
        tmp_path, monkeypatch, second_observation, expected):
    monkeypatch.setenv("TRACKING_DATA_MAX_AGE_HOURS", "18")
    store, server, base = run_server(tmp_path)
    now = datetime.now(UTC)
    observed = (now.isoformat() if second_observation == "fresh" else
                now.replace(tzinfo=None).isoformat() if second_observation == "naive" else
                (now - timedelta(days=2)).isoformat() if second_observation == "stale" else
                second_observation)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "packages": [
        {"tracking": "876543210121", "official_tracking": {"observed_at": now.isoformat()}},
        {"tracking": "876543210122", "official_tracking": {"observed_at": observed}},
    ]})
    try:
        _, detail = request(base + "/api/track/XSD1", "secret-token")
        _, listing = request(base + "/api/shipments", "secret-token")
        assert detail["shipment"]["tracking_data_stale"] == expected
        assert listing["items"][0]["tracking_data_stale"] == expected
    finally:
        server.shutdown()


@pytest.mark.parametrize("result_version,expected_attempt", [(1, None), (None, None), (3, False)])
def test_track_api_excludes_attempts_from_previous_binding_of_same_tracking(
        tmp_path, result_version, expected_attempt):
    store, server, base = run_server(tmp_path)
    tracking = "876543210121"
    package = {"tracking": tracking, "binding_version": 3,
               "binding_history": [{"from": "876543210122", "to": tracking}]}
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "packages": [package]})
    result = {"tracking": tracking, "ok": False, "observed_at": datetime.now(UTC).isoformat(),
              "error": "upstream unavailable"}
    if result_version is not None:
        result["binding_version"] = result_version
    store.put_document("ups_results", {"XSD1": {"package_results": {tracking: result}}})
    try:
        _, detail = request(base + "/api/track/XSD1", "secret-token")
        _, listing = request(base + "/api/shipments", "secret-token")
        for attempt in (detail["shipment"]["last_tracking_attempt"],
                        detail["shipment"]["packages"][0]["last_tracking_attempt"],
                        listing["items"][0]["last_tracking_attempt"]):
            if expected_attempt is None:
                assert attempt is None
            else:
                assert attempt["ok"] is expected_attempt
    finally:
        server.shutdown()
