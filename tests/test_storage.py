import json
import threading
from datetime import UTC, datetime, timedelta

from storage import Storage


def test_legacy_json_migration_is_complete_and_idempotent(tmp_path):
    (tmp_path / "shipments.json").write_text(
        json.dumps({"XSD1": {"orderNo": "XSD1", "status": "运输中"}}),
        encoding="utf-8",
    )
    (tmp_path / "sales_map.json").write_text(
        json.dumps({"XSD1": {"salesperson": "张三"}}), encoding="utf-8"
    )

    store = Storage(tmp_path)
    first = store.migrate_legacy_json()
    second = store.migrate_legacy_json()

    assert first == {"shipments": 1, "documents": 1}
    assert second == {"shipments": 0, "documents": 0}
    assert store.get_shipments()["XSD1"]["status"] == "运输中"
    assert store.get_document("sales_map", {})["XSD1"]["salesperson"] == "张三"


def test_concurrent_shipment_updates_do_not_lose_orders(tmp_path):
    store = Storage(tmp_path)
    barrier = threading.Barrier(3)

    def add(order):
        barrier.wait()
        Storage(tmp_path).upsert_shipment(order, {"orderNo": order, "status": "已预报"})

    threads = [threading.Thread(target=add, args=(order,)) for order in ("XSD1", "XSD2")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert set(store.get_shipments()) == {"XSD1", "XSD2"}


def test_message_identity_not_timestamp_controls_deduplication(tmp_path):
    store = Storage(tmp_path)
    timestamp = "2026-09-08T01:00:00Z"

    assert store.record_message("channel", "message-1", timestamp, {"body": "first"}) is True
    assert store.record_message("channel", "message-2", timestamp, {"body": "second"}) is True
    assert store.record_message("channel", "message-1", timestamp, {"body": "first"}) is False

    pending = store.pending_messages("channel")
    assert [message["id"] for message in pending] == ["message-1", "message-2"]
    store.complete_message("channel", "message-1")
    store.fail_message("channel", "message-2", "download failed")
    assert [message["id"] for message in store.pending_messages("channel")] == ["message-2"]


def test_inbox_item_survives_worker_restart_and_new_arrival(tmp_path):
    now = datetime(2026, 9, 8, tzinfo=UTC)
    store = Storage(tmp_path)
    store.enqueue_inbox("label-1", {"name": "first.png"}, now=now)
    claimed = store.claim_inbox("worker-a", now=now, lease_seconds=10)
    assert claimed["id"] == "label-1"

    store.enqueue_inbox("label-2", {"name": "second.png"}, now=now)
    assert Storage(tmp_path).claim_inbox("worker-b", now=now) ["id"] == "label-2"
    assert Storage(tmp_path).claim_inbox(
        "worker-c", now=now + timedelta(seconds=11)
    )["id"] == "label-1"


def test_failed_task_retries_then_moves_to_dead_letter(tmp_path):
    now = datetime(2026, 9, 8, tzinfo=UTC)
    store = Storage(tmp_path)
    task_id = store.enqueue_task("track", "track:XSD1:v1", {"order": "XSD1"}, now=now)

    for attempt in range(1, 4):
        task = store.claim_task("worker", now=now + timedelta(hours=attempt))
        assert task["id"] == task_id
        state = store.fail_task(task_id, "temporary", max_attempts=3,
                                now=now + timedelta(hours=attempt))

    assert state == "dead"
    assert store.claim_task("worker", now=now + timedelta(days=1)) is None
