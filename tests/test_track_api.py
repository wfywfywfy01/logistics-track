from urllib.error import HTTPError
from urllib.parse import quote
from datetime import UTC, datetime

from test_admin_server import request, run_server


def seed(store):
    store.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1ZC23W53D441751825",
        "domestic": "SF5152606658887", "salesperson": "周佳丽",
        "products": ["VERTU PHANTOM"], "status_observed_at": "2026-09-11T06:20:00+00:00",
        "history": [{"from": "已预报", "to": "运输中", "at": "2026-09-11T06:20:00+00:00",
                     "observed_at": "2026-09-11T06:20:00+00:00",
                     "tracking": "1ZC23W53D441751825",
                     "detail": "09/11/2026 ANCHORAGE Arrived at facility"}],
        "packages": [{
            "tracking": "1ZC23W53D441751825", "carrier": "UPS", "role": "primary",
            "status": "运输中", "binding_version": 1,
            "official_tracking": {
                "source": "ups.com", "observed_at": "2026-09-11T06:20:00+00:00",
                "status_en": "In Transit", "progress": "60",
                "estimated_delivery": {"local_date_text": "2026-09-13"},
                "latest_event": {"source_time_text": "09/11/2026 14:20",
                                 "occurred_at_utc": "2026-09-11T06:20:00Z",
                                 "location": "ANCHORAGE, AK, US",
                                 "status": "Arrived at facility", "description": ""},
                "events": [
                    {"source_time_text": "09/11/2026 14:20",
                     "occurred_at_utc": "2026-09-11T06:20:00Z",
                     "location": "ANCHORAGE, AK, US",
                     "status": "Arrived at facility", "description": ""},
                    {"source_time_text": "09/10/2026 09:00",
                     "occurred_at_utc": "2026-09-10T01:00:00Z",
                     "location": "SHANGHAI, CN", "status": "Departed", "description": ""}],
                "progress_steps": [],
            }}]})
    store.upsert_shipment("XSD2", {"orderNo": "XSD2", "status": "已预报", "history": []})


def test_track_by_order_returns_official_events(tmp_path):
    store, server, base = run_server(tmp_path)
    seed(store)
    try:
        status, payload = request(base + "/api/track/XSD1", "secret-token")
    finally:
        server.shutdown()

    assert status == 200 and payload["ok"] is True
    shipment = payload["shipment"]
    assert shipment["latest_event"]["location"] == "ANCHORAGE, AK, US"
    assert "ANCHORAGE" in shipment["latest_event_text"]
    assert shipment["packages"][0]["official"]["event_count"] == 2
    assert shipment["packages"][0]["official"]["source"] == "ups.com"


def test_track_by_tracking_number_and_missing_number(tmp_path):
    store, server, base = run_server(tmp_path)
    seed(store)
    try:
        status, payload = request(base + "/api/track?tracking=1ZC23W53D441751825",
                                  "secret-token")
        assert status == 200 and payload["count"] == 1
        assert payload["matches"][0]["order"] == "XSD1"
        try:
            request(base + "/api/track?tracking=1ZC0000000000000000", "secret-token")
            assert False, "unknown tracking number must be 404"
        except HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()


def test_track_path_falls_back_to_tracking_number_for_legacy_callers(tmp_path):
    store, server, base = run_server(tmp_path)
    seed(store)
    try:
        status, payload = request(base + "/api/track/1ZC23W53D441751825", "secret-token")
    finally:
        server.shutdown()

    assert status == 200
    assert payload["tracking"] == "1ZC23W53D441751825"
    assert payload["count"] == 1
    assert payload["matches"][0]["order"] == "XSD1"


def test_track_view_uses_latest_event_across_packages():
    from admin_server import track_view

    shipment = {"orderNo": "XSD1", "packages": [
        {"tracking": "876543210121", "official_tracking": {"latest_event": {
            "occurred_at_utc": "2026-09-20T08:00:00Z", "status": "In transit"}}},
        {"tracking": "876543210122", "official_tracking": {"latest_event": {
            "occurred_at_utc": "2026-09-22T08:00:00Z", "status": "Delivered"}}},
    ]}

    view = track_view(shipment)

    assert view["latest_event"]["status"] == "Delivered"
    assert view["latest_event_tracking"] == "876543210122"


def test_track_api_exposes_latest_failed_carrier_attempt(tmp_path):
    store, server, base = run_server(tmp_path)
    seed(store)
    store.put_document("ups_results", {"XSD1": {
        "tracking": "1ZC23W53D441751825", "carrier": "UPS", "ok": False,
        "observed_at": "2026-09-22T09:00:00+00:00", "error": "no GetStatus data",
    }})
    try:
        status, payload = request(base + "/api/track/XSD1", "secret-token")
    finally:
        server.shutdown()

    attempt = payload["shipment"]["packages"][0]["last_tracking_attempt"]
    assert status == 200
    assert attempt == {"observed_at": "2026-09-22T09:00:00+00:00", "ok": False,
                       "error": "no GetStatus data"}


def test_track_view_reports_stale_official_snapshot():
    from admin_server import track_view

    shipment = {"orderNo": "XSD1", "packages": [{
        "tracking": "876543210123", "official_tracking": {
            "observed_at": "2026-09-20T08:00:00+00:00", "events": []}}]}

    view = track_view(
        shipment, max_age_hours=18,
        now=datetime(2026, 9, 22, 8, 0, tzinfo=UTC))

    assert view["tracking_data_max_age_hours"] == 18
    assert view["tracking_data_stale"] is True


def test_stats_and_shipment_list(tmp_path):
    store, server, base = run_server(tmp_path)
    seed(store)
    try:
        status, stats = request(base + "/api/stats", "secret-token")
        assert status == 200 and stats["total"] == 2
        assert stats["by_status"]["运输中"] == 1
        assert stats["missing_intl"] == 1
        assert stats["with_official_events"] == 1

        status, listing = request(base + "/api/shipments?status=" + quote("运输中"),
                                  "secret-token")
        assert status == 200 and listing["total"] == 1
        row = listing["items"][0]
        assert row["order"] == "XSD1" and row["event_count"] == 2
        assert "ANCHORAGE" in row["latest_event_text"]
        assert "last_tracking_attempt" in row
        assert "tracking_data_stale" in row

        status, limited = request(base + "/api/shipments?limit=1&offset=1", "secret-token")
        assert status == 200 and limited["count"] == 1 and limited["total"] == 2
    finally:
        server.shutdown()


def test_track_api_requires_authentication(tmp_path):
    store, server, base = run_server(tmp_path)
    seed(store)
    try:
        try:
            request(base + "/api/track/XSD1")
            assert False, "missing credentials must fail"
        except HTTPError as error:
            assert error.code == 401
    finally:
        server.shutdown()
