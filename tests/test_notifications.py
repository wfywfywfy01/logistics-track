from test_reliability import load_pipeline
import subprocess
import pytest

def test_failed_notification_stays_queued_and_does_not_ack(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1", "history": [],
        "products": [], "needs_notify": True, "binding_version": 1,
    })
    monkeypatch.setattr(pipeline, "cli", lambda _args: None)
    result = pipeline.notify("channel")
    assert result["failed"] == 1
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is True
    assert pipeline.STORE.pending_task_count("notify_group") == 1

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
