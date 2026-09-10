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


def test_stale_tracking_data_is_separate_from_logistics_stall(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment(observed="2026-09-07T00:00:00+00:00"))
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": True, "observed_at": "2026-09-08T00:00:00+00:00"}})

    result = refresh_operational_tasks(
        store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48}, freshness_hours=24)

    active = store.list_tasks(("pending", "retry"))
    assert [(row["kind"], row["payload"]["reason"]) for row in active] == [
        ("tracking_stale", "official tracking data is stale")]
    assert result["freshness_threshold_hours"] == 24


def test_missing_tracking_observation_time_blocks_stall_decision(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": True}})

    refresh_operational_tasks(
        store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48}, freshness_hours=24)

    active = store.list_tasks(("pending", "retry"))
    assert [(row["kind"], row["payload"]["observed_at"]) for row in active] == [
        ("tracking_failure", "N/A")]


def test_daily_report_exposes_unconfigured_freshness_threshold(tmp_path):
    report = build_daily_report(Storage(tmp_path))
    assert report["tracking_data_max_age_hours"] == "N/A"


def test_secondary_package_staleness_blocks_order_stall_decision(tmp_path):
    store = Storage(tmp_path)
    item = shipment(observed="2026-09-10T11:00:00+00:00")
    item["packages"] = [
        {"tracking": "1Z1", "carrier": "UPS", "active": True},
        {"tracking": "876543210123", "carrier": "FEDEX", "active": True},
    ]
    store.upsert_shipment("XSD1", item)
    store.put_document("ups_results", {"XSD1": {"ok": True, "package_results": {
        "1Z1": {"tracking": "1Z1", "carrier": "UPS", "ok": True,
                "observed_at": "2026-09-10T11:30:00+00:00"},
        "876543210123": {"tracking": "876543210123", "carrier": "FEDEX", "ok": True,
                         "observed_at": "2026-09-08T00:00:00+00:00"},
    }}})

    refresh_operational_tasks(
        store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48, "FEDEX": 48},
        freshness_hours=24)

    active = store.list_tasks(("pending", "retry"))
    assert [(row["kind"], row["payload"]["tracking"], row["payload"]["carrier"])
            for row in active] == [("tracking_stale", "876543210123", "FEDEX")]


def test_daily_freshness_counts_each_package_carrier(tmp_path):
    store = Storage(tmp_path)
    store.put_document("ups_results", {"XSD1": {"ok": True, "package_results": {
        "1Z1": {"tracking": "1Z1", "carrier": "UPS", "ok": True,
                "observed_at": "2026-09-10T11:30:00+00:00"},
        "876543210123": {"tracking": "876543210123", "carrier": "FEDEX", "ok": True,
                         "observed_at": "2026-09-08T00:00:00+00:00"},
    }}})

    report = build_daily_report(store)

    assert report["carrier_freshness"]["UPS"]["total"] == 1
    assert report["carrier_freshness"]["FEDEX"]["latest_observed_at"] == \
        "2026-09-08T00:00:00+00:00"
