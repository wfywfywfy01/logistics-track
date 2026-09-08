from test_reliability import load_pipeline

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
