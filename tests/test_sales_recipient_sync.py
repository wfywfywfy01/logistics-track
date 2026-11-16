from copy import deepcopy

import pytest

from test_reliability import load_pipeline


OLD_EVENT = "2026-11-15T00:00:00+00:00"
LATEST_EVENT = "2026-11-16T00:00:00+00:00"


def seed_order(pipeline):
    shipment = {
        "orderNo": "XSD1", "status": "签收", "salesperson": "刘佳鑫",
        "salesperson_id": 12549, "sales_recipient_ids": [12549],
        "intl": "876543210121", "domestic": "SF1234567890123", "products": ["手机"],
        "binding_version": 2, "binding_history": [{"from": "old", "to": "876543210121"}],
        "needs_notify": False, "notified_status": "签收",
        "dm_notified_status": "签收", "dm_notified_event": LATEST_EVENT,
        "history": [
            {"from": "已出国际单", "to": "运输中", "at": OLD_EVENT},
            {"from": "运输中", "to": "签收", "at": LATEST_EVENT},
        ],
    }
    pipeline.STORE.upsert_shipment("XSD1", shipment)
    pipeline.STORE.put_document("org_people", {"何川": [12537], "刘佳鑫": [12549]})
    return pipeline.STORE.get_shipment("XSD1")


def fresh_sales(*args, **kwargs):
    return {"salesperson": "另一主要人员", "sales_recipient_ids": [12537, 12549],
            "real_order": "XSD1", "domestic": "OTHER", "products": "OTHER"}


def test_default_sync_is_read_only_even_when_replay_requested(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    before = seed_order(pipeline)
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)

    pipeline.sync_sales_recipients(["XSD1"], replay_latest=True, recipient_uid=12537)

    assert pipeline.STORE.get_shipment("XSD1") == before
    assert pipeline.STORE.list_tasks() == []


def test_apply_sync_updates_participants_without_rewriting_order_or_sending(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    before = seed_order(pipeline)
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)
    monkeypatch.setattr(pipeline, "cli", lambda args: pytest.fail("Sync must not send directly."))

    pipeline.sync_sales_recipients(["XSD1"], apply=True)

    after = pipeline.STORE.get_shipment("XSD1")
    assert after["sales_recipient_ids"] == [12537, 12549]
    for key, value in before.items():
        if key != "sales_recipient_ids":
            assert after[key] == value, key
    assert pipeline.STORE.list_tasks() == []


def test_replay_only_latest_event_to_requested_uid_is_idempotent(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    before = seed_order(pipeline)
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)
    monkeypatch.setattr(pipeline, "cli", lambda args: pytest.fail("Replay must enqueue instead of sending."))

    for _ in range(2):
        pipeline.sync_sales_recipients(["XSD1"], apply=True, replay_latest=True, recipient_uid=12537)

    tasks = pipeline.STORE.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "notify_dm"
    assert tasks[0]["payload"]["uid"] == 12537
    assert tasks[0]["payload"]["event_key"] == LATEST_EVENT
    assert tasks[0]["payload"]["status"] == "签收"
    assert pipeline.STORE.get_shipment("XSD1")["history"] == before["history"]
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is False


def test_replay_never_resends_primary_with_existing_legacy_receipt(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    seed_order(pipeline)
    legacy = pipeline.STORE.enqueue_task("notify_dm", f"dm:XSD1:{LATEST_EVENT}", {
        "order": "XSD1", "name": "刘佳鑫", "uid": 12549,
        "status": "签收", "event_key": LATEST_EVENT,
    })
    pipeline.STORE.complete_notification(legacy, "XSD1", LATEST_EVENT, {}, "ok")
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)

    pipeline.sync_sales_recipients(["XSD1"], apply=True, replay_latest=True)

    tasks = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert len(tasks) == 2
    assert next(task for task in tasks if task["id"] == legacy)["status"] == "succeeded"
    pending = [task for task in tasks if task["status"] == "pending"]
    assert len(pending) == 1 and pending[0]["payload"]["uid"] == 12537
    assert not pipeline.STORE.list_tasks(kinds=("notify_group",))


@pytest.mark.parametrize("history", [[], [{}], [
    {"from": "已预报", "to": "已出国际单", "at": LATEST_EVENT, "notification_suppressed": True},
]])
def test_sync_does_not_invent_or_replay_suppressed_events(monkeypatch, tmp_path, history):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    shipment = seed_order(pipeline)
    shipment["history"] = deepcopy(history)
    pipeline.STORE.upsert_shipment("XSD1", shipment)
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)

    pipeline.sync_sales_recipients(["XSD1"], apply=True, replay_latest=True)

    assert pipeline.STORE.get_shipment("XSD1")["sales_recipient_ids"] == [12537, 12549]
    assert pipeline.STORE.get_shipment("XSD1")["history"] == history
    assert pipeline.STORE.list_tasks() == []


@pytest.mark.parametrize("failure", ["missing", "exception"])
def test_failed_sales_refresh_preserves_existing_identity_and_timestamp(monkeypatch, tmp_path, failure):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    before = seed_order(pipeline)

    def fail(*args, **kwargs):
        if failure == "exception":
            raise RuntimeError("sales unavailable")
        return {}

    monkeypatch.setattr(pipeline, "match_sales", fail)

    pipeline.sync_sales_recipients(["XSD1"], apply=True, replay_latest=True)

    assert pipeline.STORE.get_shipment("XSD1") == before
    assert pipeline.STORE.list_tasks(kinds=("notify_group", "notify_dm")) == []


def test_recipient_filter_cannot_add_someone_absent_from_authoritative_participants(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    seed_order(pipeline)
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)

    pipeline.sync_sales_recipients(["XSD1"], apply=True, replay_latest=True, recipient_uid=99999)

    assert pipeline.STORE.list_tasks() == []


def test_sales_refresh_does_not_overwrite_concurrent_shipment_update(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    seed_order(pipeline)

    def query(*args, **kwargs):
        def update(shipment):
            shipment["note"] = "运营刚补录的备注"
            shipment["binding_version"] = 3
            return shipment, []
        pipeline.STORE.mutate_shipment("XSD1", update)
        return fresh_sales()

    monkeypatch.setattr(pipeline, "match_sales", query)

    pipeline.sync_sales_recipients(["XSD1"], apply=True)

    after = pipeline.STORE.get_shipment("XSD1")
    assert after["sales_recipient_ids"] == [12537, 12549]
    assert after["note"] == "运营刚补录的备注"
    assert after["binding_version"] == 3


def test_source_recovery_catches_up_each_event_missing_participants(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "salesperson": "刘佳鑫",
        "salesperson_id": 12549, "history": [], "intl": "876543210121"})
    monkeypatch.setattr(pipeline, "match_sales", lambda *a, **k: {})
    pipeline.sync_sales_recipients(["XSD1"], apply=True)
    pipeline.track_update("XSD1", "异常", observed_at=OLD_EVENT)
    pipeline.track_update("XSD1", "签收", observed_at=LATEST_EVENT)
    monkeypatch.setattr(pipeline, "match_sales", fresh_sales)

    pipeline.sync_sales_recipients(["XSD1"], apply=True)
    pipeline.sync_sales_recipients(["XSD1"], apply=True)

    tasks = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert sorted((task["payload"]["uid"], task["payload"]["status"]) for task in tasks) == sorted([
        (12537, "异常"), (12549, "异常"), (12537, "签收"), (12549, "签收")])
    assert not any(event.get("sales_recipients_pending")
                   for event in pipeline.STORE.get_shipment("XSD1")["history"])
