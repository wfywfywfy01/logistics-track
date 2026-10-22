from datetime import UTC, datetime, timedelta

import pytest

from storage import Storage, TaskConflict
from test_reliability import load_auto_track, load_pipeline, load_watcher


def test_legacy_update_rejects_observation_older_than_unchanged_status(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "status": "运输中", "intl": "1Z1", "history": [],
        "status_observed_at": "2026-10-20T01:00:00Z"})
    pipeline.track_update("XSD1", "运输中", observed_at="2026-10-20T03:00:00Z")
    result = pipeline.track_update("XSD1", "异常", observed_at="2026-10-20T02:00:00Z")
    assert result["reason"] == "stale observation"
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "运输中"
    assert not pipeline.STORE.list_tasks(kinds=("notify_group",))


def test_forced_legacy_rebind_restarts_terminal_package(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "match_sales", lambda *args, **kwargs: {})
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "签收",
        "intl": "876543210121", "binding_version": 1, "history": [], "products": []})
    pipeline.ingest_pair("XSD1", "876543210122", force=True)
    result = pipeline.package_update("XSD1", "876543210122", "运输中", binding_version=2)
    assert result["changed"] is True
    package = pipeline.STORE.get_shipment("XSD1")["packages"][0]
    assert package["binding_history"][0]["snapshot"]["status"] == "签收"


def test_sales_backfill_preserves_concurrently_created_order(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    original = {"orderNo": "XSD1", "status": "运输中", "intl": "1Z1",
                "history": [{"at": "existing-event"}], "salesperson": "已确认人员"}
    def sales(*args, **kwargs):
        pipeline.STORE.upsert_shipment("XSD1", original)
        return {"salesperson": "销售系统人员", "products": "Phone"}
    monkeypatch.setattr(pipeline, "match_sales", sales)
    assert pipeline._create_order_from_sales("XSD1") == original
    assert not pipeline.STORE.list_audit("XSD1")


def test_silent_backfill_preserves_other_pending_notifications(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "match_sales", lambda *args, **kwargs: {})
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [{"at": "earlier"}], "needs_notify": True})
    pipeline.STORE.enqueue_task("review", "pair-review:XSD1:876543210121",
                               {"order": "XSD1", "intl": "876543210121"})
    prior = pipeline.STORE.enqueue_task("notify_group", "prior", {"order": "XSD1"})
    pipeline.backfill_pairs(silent=True)
    pipeline._queue_notifications("channel")
    tasks = pipeline.STORE.list_tasks(kinds=("notify_group", "notify_dm"))
    assert len(tasks) == 1 and tasks[0]["id"] == prior and tasks[0]["status"] == "pending"
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is True


def test_family_recipients_persist_every_event_before_notification_drain(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    for order, name in (("XSD1", "张三"), ("XSD1-1", "李四")):
        pipeline.STORE.upsert_shipment(order, {"orderNo": order, "status": "运输中",
            "intl": "1Z1", "salesperson": name, "history": [], "products": []})
    pipeline.track_update("XSD1", "异常", observed_at="2026-10-20T01:00:00Z")
    pipeline.track_update("XSD1", "运输中", observed_at="2026-10-20T02:00:00Z")
    tasks = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert len(tasks) == 4
    assert {(row["payload"]["name"], row["payload"]["status"]) for row in tasks} == {
        (name, status) for name in ("张三", "李四") for status in ("异常", "运输中")}


def test_legacy_dm_task_is_not_reenqueued_under_new_recipient_key(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "1Z1", "salesperson": "张三", "history": [{"at": "event-1"}],
        "needs_notify": True, "products": []})
    pipeline.STORE.enqueue_task("notify_dm", "dm:XSD1:event-1", {
        "order": "XSD1", "event_key": "event-1", "name": "张三"})
    pipeline._queue_notifications("channel")
    assert len(pipeline.STORE.list_tasks(kinds=("notify_dm",))) == 1


@pytest.mark.parametrize("state", ["unknown", "succeeded"])
def test_notification_retry_cannot_replay_ambiguous_or_delivered_task(tmp_path, state):
    store = Storage(tmp_path)
    task_id = store.enqueue_task("notify_dm", "notification", {})
    if state == "unknown":
        store.mark_task_unknown(task_id, "receipt missing")
    else:
        store.complete_task(task_id)
    with pytest.raises(TaskConflict):
        store.act_on_task(task_id, "retry", "ops", "retry clicked")
    assert store.list_tasks()[0]["status"] == state


@pytest.mark.parametrize("operation", ["complete", "fail", "inflight", "receipt"])
def test_old_worker_cannot_modify_reclaimed_task(tmp_path, operation):
    store = Storage(tmp_path)
    now = datetime.now(UTC)
    task_id = store.enqueue_task("notify_group", "notification", {}, now=now)
    first = store.claim_task("worker-a", now=now, lease_seconds=1)
    second = store.claim_task("worker-b", now=now + timedelta(seconds=2))
    with pytest.raises(TaskConflict):
        if operation == "complete":
            store.complete_task(task_id, claim=first)
        elif operation == "fail":
            store.fail_task(task_id, "stale failure", claim=first)
        elif operation == "inflight":
            store.mark_delivery_inflight(task_id, claim=first)
        else:
            store.complete_notification(task_id, "XSD1", "event", {}, claim=first)
    row = store.list_tasks()[0]
    assert row["status"] == "running" and row["lease_owner"] == second["lease_owner"]


def test_old_ocr_worker_cannot_enqueue_followups_after_reclaim(tmp_path):
    store = Storage(tmp_path)
    now = datetime.now(UTC)
    store.enqueue_inbox("image", {}, now=now)
    first = store.claim_inbox("worker-a", now=now, lease_seconds=1)
    store.claim_inbox("worker-b", now=now + timedelta(seconds=2))
    with pytest.raises(TaskConflict):
        store.complete_inbox_with_task("image", "pipeline", "stale", {}, claim=first)
    assert not store.list_tasks()
    assert store.get_inbox()[0]["lease_owner"] == "worker-b"


def test_failed_message_retry_uses_composite_identity_and_audits_atomically(tmp_path):
    store = Storage(tmp_path)
    for channel in ("a", "b"):
        store.record_message(channel, "m:1", "2026-10-20T01:00:00Z", {"body": "input"})
        store.fail_message(channel, "m:1", "timeout", max_attempts=1)
    rows, total = store.issues_page(kind="incoming_message")
    assert total == 2 and {row["channel_id"] for row in rows} == {"a", "b"}
    assert store.retry_message("a", "m:1", "ops", "upstream recovered")["status"] == "pending"
    assert store.pending_messages("a")[0]["attempts"] == 0
    assert not store.pending_messages("b")
    with pytest.raises(TaskConflict):
        store.retry_message("a", "m:1", "ops", "double click")
    with store.connect() as connection:
        connection.execute("""CREATE TRIGGER reject_retry_audit BEFORE INSERT ON audit_log
            BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    with pytest.raises(Exception, match="audit unavailable"):
        store.retry_message("b", "m:1", "ops", "retry")
    assert not store.pending_messages("b")


def test_pipeline_lease_covers_configured_total_step_timeout(monkeypatch, tmp_path):
    import types
    auto = load_auto_track(monkeypatch, tmp_path)
    monkeypatch.setenv("TRACK_STEP_TIMEOUT_SECONDS", "8000")
    monkeypatch.setenv("STEP_TIMEOUT_SECONDS", "1000")
    task_id = auto.STORE.enqueue_task("pipeline", "work", {})
    claims = []
    claim_task = auto.STORE.claim_task
    def capture(*args, **kwargs):
        claims.append(kwargs["lease_seconds"])
        return claim_task(*args, **kwargs)
    monkeypatch.setattr(auto.STORE, "claim_task", capture)
    monkeypatch.setattr(auto, "run", lambda *args: True)
    options = types.SimpleNamespace(queued_only=True, skip_track=False, mode="full",
                                    channel_id="channel", bot_app_id="bot")
    assert auto.execute(options) == 0
    assert min(claims) >= 8000 + 5 * 1000
    assert auto.STORE.list_tasks()[0]["status"] == "succeeded"


def test_watcher_forecast_and_pair_timeout_leave_message_retryable(monkeypatch, tmp_path):
    import re
    import subprocess
    watcher = load_watcher(monkeypatch, tmp_path)
    url = "https://example.invalid/forecast-attachment"
    name = "forecast.xlsx"
    uid = re.sub(r"[^A-Za-z0-9-]", "", url)[-12:]
    (tmp_path / "tmp" / (uid + "_" + name)).write_bytes(b"PK\x03\x04")
    timeouts = []
    def timed_out(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired("pipeline", kwargs["timeout"])
    monkeypatch.setattr(watcher.subprocess, "run", timed_out)
    monkeypatch.setattr(watcher, "fetch_history", lambda *args: "2026-10-20T01:00:00Z")
    watcher.STORE.record_message("channel", "m1", "2026-10-20T01:00:00Z", {
        "attachments": [{"name": name, "url": url, "attachment_type": "file"}],
        "body": "XSD1==876543210121"})
    watcher.watch_once("channel", "bot", "2026-10-20T00:00:00Z")
    pending = watcher.STORE.pending_messages("channel")
    assert len(pending) == 1 and pending[0]["attempts"] == 1
    assert timeouts == [300, 120]


def test_family_receipt_does_not_ack_primary_recipient(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"salesperson": "张三", "history": [{"at": "event-1"}]})
    task_id = store.enqueue_task("notify_dm", "family", {"name": "李四"})
    store.complete_notification(task_id, "XSD1", "event-1", {
        "dm_notified_event": "event-1", "dm_notified_status": "运输中"}, "ok")
    assert "dm_notified_event" not in store.get_shipment("XSD1")


@pytest.mark.parametrize("description", ["Not delivered", "Undelivered",
    "Shipment not delivered", "Package will be delivered tomorrow"])
def test_pipeline_cannot_turn_negative_or_future_delivery_into_signed(monkeypatch, tmp_path, description):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "1Z1", "packages": [{"tracking": "1Z1", "status": "运输中"}]})
    for update in (lambda: pipeline.track_update("XSD1", description),
                   lambda: pipeline.package_update("XSD1", "1Z1", description)):
        assert update()["reason"] == "unknown status"
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "运输中"
    assert not pipeline.STORE.list_tasks()
    assert pipeline.norm_status("Shipment has been delivered") == "签收"


def test_storage_context_closes_connection_after_commit_and_rollback(tmp_path):
    import sqlite3
    store = Storage(tmp_path)
    with store.connect() as committed:
        committed.execute("INSERT INTO documents VALUES('committed','{}','now')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        committed.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="abort"):
        with store.connect() as aborted:
            aborted.execute("INSERT INTO documents VALUES('aborted','{}','now')")
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        aborted.execute("SELECT 1")
    assert store.get_document("committed") == {}
    assert store.get_document("aborted") is None
    direct = store.connect()
    try:
        assert direct.execute("SELECT 1").fetchone()[0] == 1
    finally:
        direct.close()
