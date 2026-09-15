from test_reliability import load_pipeline
import subprocess
import pytest

def test_empty_delivery_receipt_is_held_unknown_and_does_not_ack(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: None)
    result = pipeline.notify("channel")
    assert result["failed"] == 1
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is True
    assert pipeline.STORE.pending_task_count("notify_group") == 0
    assert pipeline.STORE.task_counts()["notify_group"]["unknown"] == 1

def test_successful_notification_is_acknowledged(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: "ok")
    result = pipeline.notify("channel")
    assert result["notified"] == 1
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is False
    assert pipeline.STORE.pending_task_count("notify_group") == 0


def test_business_failure_response_is_not_acknowledged(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: '{"ok":false,"error":"rejected"}')

    result = pipeline.notify("channel")

    assert result["failed"] == 1
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is True
    assert pipeline.STORE.pending_task_count("notify_group") == 1


def test_unrecognized_receipt_is_held_unknown_without_automatic_retry(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "异常", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: "gateway accepted")

    result = pipeline.notify("channel")

    assert result["failed"] == 1
    assert pipeline.STORE.task_counts()["notify_group"]["unknown"] == 1
    assert pipeline.STORE.pending_task_count("notify_group") == 0


def test_receipt_commit_failure_is_held_unknown_without_resend(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: "ok")
    monkeypatch.setattr(pipeline.STORE, "complete_notification",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db busy")))

    result = pipeline.notify("channel")

    assert result["failed"] == 1
    assert pipeline.STORE.task_counts()["notify_group"]["unknown"] == 1


def test_exception_uses_unified_group_notification(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "异常", "intl": "1Z1",
        "history": [{"from": "运输中", "to": "异常", "at": "event-1"}],
        "products": [], "needs_notify": True, "binding_version": 1,
    })

    queued = pipeline._queue_notifications("channel")
    task = pipeline.STORE.list_tasks(kinds=("notify_group",))[0]

    assert queued[0]["line"].startswith("⚠️")
    assert task["payload"]["body"] == queued[0]["line"]


def test_rapid_status_changes_persist_each_notification_in_same_transaction(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "binding_version": 1,
    })

    pipeline.track_update("XSD1", "异常", observed_at="2026-09-10T01:00:00Z")
    pipeline.track_update("XSD1", "运输中", observed_at="2026-09-10T02:00:00Z")

    tasks = pipeline.STORE.list_tasks(("pending",), kinds=("notify_group",))
    assert [task["payload"]["status"] for task in reversed(tasks)] == ["异常", "运输中"]


def test_notification_enqueue_failure_rolls_back_status_change(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "binding_version": 1,
    })
    with pipeline.STORE.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_notify BEFORE INSERT ON tasks
               WHEN NEW.kind='notify_group' BEGIN SELECT RAISE(ABORT,'queue unavailable'); END""")

    with pytest.raises(Exception, match="queue unavailable"):
        pipeline.track_update("XSD1", "异常", observed_at="2026-09-10T01:00:00Z")

    assert pipeline.STORE.get_shipment("XSD1")["status"] == "运输中"


def test_cli_nonzero_without_explicit_rejection_is_unknown(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline.robust, "cli_run", lambda _args: (1, "", "rejected"))

    result = pipeline.notify("channel")

    assert result["failed"] == 1
    assert pipeline.STORE.pending_task_count("notify_group") == 0
    assert pipeline.STORE.task_counts()["notify_group"]["unknown"] == 1


def test_cli_nonzero_explicit_rejection_is_retryable(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(
        pipeline.robust, "cli_run",
        lambda _args: (1, '{"ok":false,"error":"rejected"}', ""))

    result = pipeline.notify("channel")

    assert result["failed"] == 1
    assert pipeline.STORE.pending_task_count("notify_group") == 1


def test_unknown_delivery_outcome_is_held_for_manual_review(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("vertu-cli", 120)))

    result = pipeline.notify("channel")

    assert result["failed"] == 1
    assert pipeline.STORE.pending_task_count("notify_group") == 0
    assert pipeline.STORE.task_counts()["notify_group"]["unknown"] == 1


def test_process_crash_during_send_leaves_notification_unknown(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: (_ for _ in ()).throw(SystemExit(9)))

    with pytest.raises(SystemExit):
        pipeline.notify("channel")

    assert pipeline.STORE.task_counts()["notify_group"]["unknown"] == 1


def test_manual_confirmation_of_unknown_group_receipt_acknowledges_event(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("vertu-cli", 120)))
    pipeline.notify("channel")
    task = pipeline.STORE.list_tasks(("unknown",), kinds=("notify_group",))[0]

    pipeline.STORE.act_on_task(task["id"], "resolve", "ops", "confirmed in IM")

    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is False
