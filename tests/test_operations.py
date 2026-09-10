from datetime import UTC, datetime, timedelta

from operations import build_daily_report, refresh_operational_tasks
from storage import Storage


def shipment(status="运输中", observed="2026-09-07T00:00:00+00:00"):
    return {"orderNo": "XSD1", "intl": "1Z1", "carrier": "UPS", "status": status,
            "status_observed_at": observed, "history": []}


def test_tracking_failure_and_stall_are_distinct_and_deduplicated(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T00:00:00+00:00", "error": "timeout"}})
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)

    refresh_operational_tasks(store, now, {"UPS": 48})
    refresh_operational_tasks(store, now, {"UPS": 48})

    active = store.list_tasks(("pending", "retry"))
    assert [(row["kind"], row["payload"]["reason"]) for row in active] == [
        ("tracking_failure", "official tracking failed")]


def test_stall_task_resolves_after_status_moves(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": True, "observed_at": "2026-09-10T11:00:00+00:00"}})
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    refresh_operational_tasks(store, now, {"UPS": 48})
    assert store.task_counts()["stalled"]["pending"] == 1

    store.patch_shipment("XSD1", {"status_observed_at": "2026-09-10T11:30:00+00:00"})
    refresh_operational_tasks(store, now, {"UPS": 48})
    assert store.task_counts()["stalled"]["succeeded"] == 1


def test_missing_threshold_blocks_stall_decision_and_report_has_sources(tmp_path):
    store = Storage(tmp_path)
    item = shipment(status="签收", observed="2026-09-10T01:00:00+00:00")
    item["history"] = [{"to": "签收", "at": "2026-09-10T01:00:00+00:00"}]
    store.upsert_shipment("XSD1", item)
    store.put_document("ups_results", {"XSD1": {"carrier": "UPS", "ok": True,
        "observed_at": "2026-09-10T02:00:00+00:00"}})

    result = refresh_operational_tasks(store, datetime(2026, 9, 10, 12, tzinfo=UTC), {})
    report = build_daily_report(store, datetime(2026, 9, 10, 12, tzinfo=UTC))

    assert result["thresholds"]["UPS"] == "N/A"
    assert "stalled" not in store.task_counts()
    assert report["denominator"] == 1
    assert report["delivered_today"]["count"] == 1
    assert report["source"] == "shipments.db + ups_results document"
