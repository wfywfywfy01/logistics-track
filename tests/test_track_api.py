from urllib.error import HTTPError
from urllib.parse import quote

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
