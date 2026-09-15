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


def test_tracking_failure_keeps_one_current_task_when_observation_changes(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T00:00:00+00:00", "error": "timeout"}})
    refresh_operational_tasks(store, now, {"UPS": 48})

    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T01:00:00+00:00", "error": "proxy down"}})
    refresh_operational_tasks(store, now, {"UPS": 48})

    active = store.list_tasks(("pending", "retry", "running"), kinds=("tracking_failure",))
    assert len(active) == 1
    assert active[0]["dedupe_key"] == "tracking-failure:XSD1:1Z1"
    assert active[0]["payload"]["observed_at"] == "2026-09-10T01:00:00+00:00"
    assert active[0]["payload"]["error"] == "proxy down"


def test_tracking_refresh_preserves_task_owner(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T00:00:00+00:00", "error": "timeout"}})
    refresh_operational_tasks(store, now, {"UPS": 48})
    task = store.list_tasks(("pending",), kinds=("tracking_failure",))[0]
    store.act_on_task(task["id"], "claim", "operator-1", "负责核查官网异常")

    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T01:00:00+00:00", "error": "proxy down"}})
    refresh_operational_tasks(store, now, {"UPS": 48})

    current = store.list_tasks(("pending",), kinds=("tracking_failure",))[0]
    assert current["payload"]["owner"] == "operator-1"


def test_tracking_refresh_migrates_owner_from_legacy_timestamped_task(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    task_id = store.enqueue_task(
        "tracking_failure", "tracking-failure:XSD1:1Z1:2026-09-10T00:00:00+00:00",
        {"order": "XSD1", "tracking": "1Z1", "error": "timeout"})
    store.act_on_task(task_id, "claim", "operator-1", "负责核查官网异常")
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T01:00:00+00:00", "error": "proxy down"}})

    refresh_operational_tasks(store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48})

    current = store.list_tasks(("pending",), kinds=("tracking_failure",))
    assert len(current) == 1
    assert current[0]["dedupe_key"] == "tracking-failure:XSD1:1Z1"
    assert current[0]["payload"]["owner"] == "operator-1"


def test_tracking_failure_can_recur_after_official_tracking_recovers(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    failed = {"XSD1": {"tracking": "1Z1", "carrier": "UPS", "ok": False,
                        "observed_at": "2026-09-10T00:00:00+00:00", "error": "timeout"}}
    store.put_document("ups_results", failed)
    refresh_operational_tasks(store, now, {"UPS": 48})
    first_id = store.list_tasks(("pending",), kinds=("tracking_failure",))[0]["id"]

    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": True, "observed_at": "2026-09-10T11:30:00+00:00"}})
    refresh_operational_tasks(store, now, {"UPS": 48})
    assert store.pending_task_count("tracking_failure") == 0

    store.put_document("ups_results", failed)
    refresh_operational_tasks(store, now, {"UPS": 48})
    active = store.list_tasks(("pending",), kinds=("tracking_failure",))
    assert len(active) == 1
    assert active[0]["id"] != first_id


def test_refresh_resolves_legacy_timestamped_tracking_failure_duplicates(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    for observed in ("2026-09-10T00:00:00+00:00", "2026-09-10T01:00:00+00:00"):
        store.enqueue_task("tracking_failure", f"tracking-failure:XSD1:1Z1:{observed}", {
            "order": "XSD1", "tracking": "1Z1", "observed_at": observed})
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": False, "observed_at": "2026-09-10T02:00:00+00:00", "error": "timeout"}})

    refresh_operational_tasks(store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48})

    active = store.list_tasks(("pending", "retry", "running"), kinds=("tracking_failure",))
    assert len(active) == 1
    assert active[0]["dedupe_key"] == "tracking-failure:XSD1:1Z1"


def test_tracking_tasks_close_when_tracking_is_removed_or_order_is_deleted(tmp_path):
    store = Storage(tmp_path)
    for order in ("XSD1", "XSD2"):
        item = shipment()
        item["orderNo"] = order
        store.upsert_shipment(order, item)
        store.enqueue_task("tracking_failure", f"tracking-failure:{order}:1Z1", {
            "order": order, "tracking": "1Z1", "reason": "official tracking failed"})
    store.patch_shipment("XSD1", {"intl": "", "packages": []})
    with store.connect() as connection:
        connection.execute("DELETE FROM shipments WHERE order_no='XSD2'")

    refresh_operational_tasks(store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48})

    assert store.pending_task_count("tracking_failure") == 0


def test_tracking_tasks_close_when_all_packages_are_inactive(tmp_path):
    store = Storage(tmp_path)
    item = shipment()
    item["packages"] = [{"tracking": "1Z1", "carrier": "UPS", "active": False}]
    store.upsert_shipment("XSD1", item)
    store.enqueue_task("tracking_failure", "tracking-failure:XSD1:1Z1", {
        "order": "XSD1", "tracking": "1Z1", "reason": "official tracking failed"})
    store.put_document("ups_results", {"XSD1": {"ok": False, "package_results": {
        "1Z1": {"tracking": "1Z1", "carrier": "UPS", "ok": False,
                "observed_at": "2026-09-10T00:00:00+00:00", "error": "timeout"}}}})

    refresh_operational_tasks(store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48})

    assert store.pending_task_count("tracking_failure") == 0


def test_recovered_dead_tracking_task_is_no_longer_actionable(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment())
    task_id = store.enqueue_task("tracking_failure", "tracking-failure:XSD1:1Z1:legacy", {
        "order": "XSD1", "tracking": "1Z1", "reason": "official tracking failed"})
    store.claim_task("test", kind="tracking_failure")
    store.fail_task(task_id, "handling failed", max_attempts=1)
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": True, "observed_at": "2026-09-10T11:30:00+00:00"}})

    refresh_operational_tasks(store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48})

    task = store.list_tasks(kinds=("tracking_failure",))[0]
    assert task["status"] == "succeeded"


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


def test_failed_package_does_not_hide_another_package_stall(tmp_path):
    store = Storage(tmp_path)
    item = shipment(observed="2026-09-07T00:00:00+00:00")
    item["packages"] = [
        {"tracking": "1Z1", "carrier": "UPS", "status": "运输中", "active": True},
        {"tracking": "876543210123", "carrier": "FEDEX", "status": "运输中", "active": True},
    ]
    store.upsert_shipment("XSD1", item)
    store.put_document("ups_results", {"XSD1": {"package_results": {
        "1Z1": {"tracking": "1Z1", "carrier": "UPS", "ok": False,
                "observed_at": "2026-09-10T11:00:00Z", "error": "timeout"},
        "876543210123": {"tracking": "876543210123", "carrier": "FEDEX", "ok": True,
                         "stage": "运输中", "observed_at": "2026-09-10T11:00:00Z",
                         "latest_event": {"occurred_at_utc": "2026-09-07T00:00:00Z"}},
    }}})

    refresh_operational_tasks(
        store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48, "FEDEX": 48})

    active = store.list_tasks(("pending",))
    assert {(row["kind"], row["payload"]["tracking"]) for row in active} == {
        ("tracking_failure", "1Z1"), ("stalled", "876543210123")}


def test_daily_freshness_counts_each_package_carrier(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中", "packages": [
        {"tracking": "1Z1", "carrier": "UPS", "active": True},
        {"tracking": "876543210123", "carrier": "FEDEX", "active": True}]})
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


def test_recent_official_scan_prevents_false_stall_when_stage_is_unchanged(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", shipment(observed="2026-09-07T00:00:00+00:00"))
    store.put_document("ups_results", {"XSD1": {"tracking": "1Z1", "carrier": "UPS",
        "ok": True, "stage": "运输中", "observed_at": "2026-09-10T11:30:00+00:00",
        "latest_event": {"occurred_at_utc": "2026-09-10T11:00:00Z"}}})

    refresh_operational_tasks(
        store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48})

    assert "stalled" not in store.task_counts()


def test_stall_is_evaluated_per_package_and_carrier(tmp_path):
    store = Storage(tmp_path)
    item = shipment(observed="2026-09-10T11:00:00+00:00")
    item["packages"] = [
        {"tracking": "1Z1", "carrier": "UPS", "status": "运输中", "active": True},
        {"tracking": "876543210123", "carrier": "FEDEX", "status": "运输中", "active": True},
    ]
    store.upsert_shipment("XSD1", item)
    store.put_document("ups_results", {"XSD1": {"ok": True, "package_results": {
        "1Z1": {"tracking": "1Z1", "carrier": "UPS", "ok": True, "stage": "运输中",
                "observed_at": "2026-09-10T11:30:00+00:00",
                "latest_event": {"occurred_at_utc": "2026-09-10T11:00:00Z"}},
        "876543210123": {"tracking": "876543210123", "carrier": "FEDEX", "ok": True,
                         "stage": "运输中", "observed_at": "2026-09-10T11:30:00+00:00",
                         "latest_event": {"occurred_at_utc": "2026-09-07T00:00:00Z"}},
    }}})

    refresh_operational_tasks(
        store, datetime(2026, 9, 10, 12, tzinfo=UTC), {"UPS": 48, "FEDEX": 48})

    active = store.list_tasks(("pending",), kinds=("stalled",))
    assert [(row["payload"]["tracking"], row["payload"]["carrier"])
            for row in active] == [("876543210123", "FEDEX")]


def test_daily_report_counts_all_unresolved_tasks_beyond_preview_limit(tmp_path):
    store = Storage(tmp_path)
    for number in range(501):
        store.enqueue_task("review", "review:%d" % number, {"order": "XSD%d" % number})

    report = build_daily_report(store)

    assert report["unresolved"]["count"] == 501
    assert len(report["unresolved"]["task_ids"]) == 500


def test_daily_report_separates_status_history_and_eta_coverage(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中", "packages": [
        {"tracking": "1Z1", "carrier": "UPS", "active": True},
        {"tracking": "1Z2", "carrier": "UPS", "active": True},
        {"tracking": "1Z3", "carrier": "UPS", "active": True}]})
    store.put_document("ups_results", {"XSD1": {"ok": True, "package_results": {
        "1Z1": {"tracking": "1Z1", "carrier": "UPS", "ok": True,
                "observed_at": "2026-09-10T11:30:00+00:00", "events": [{}],
                "estimated_delivery": {"date": "2026-09-11"}},
        "1Z2": {"tracking": "1Z2", "carrier": "UPS", "ok": True,
                "observed_at": "2026-09-10T11:30:00+00:00"},
    }}})

    report = build_daily_report(store)

    assert report["carrier_freshness"]["UPS"] == {
        "total": 3, "ok": 2, "missing_result": 1, "with_events": 1,
        "with_estimated_delivery": 1,
        "latest_observed_at": "2026-09-10T11:30:00+00:00"}
