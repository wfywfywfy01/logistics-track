import sqlite3

import pytest

from test_reliability import load_pipeline


@pytest.mark.parametrize("update", ["forecast", "pair", "rematch"])
def test_automatic_salesperson_change_cannot_keep_previous_user_id(monkeypatch, tmp_path, update):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报" if update == "pair" else "运输中",
        "intl": "876543210121", "salesperson": "旧人员", "salesperson_id": "old-user",
        "history": [], "products": []})
    monkeypatch.setattr(pipeline, "match_sales", lambda *args, **kwargs: {"salesperson": "新人员"})
    monkeypatch.setattr(pipeline, "parse_forecast", lambda path: [{"orderNo": "XSD1"}])
    if update == "forecast":
        pipeline.ingest_forecast("forecast.xlsx")
    elif update == "pair":
        pipeline.ingest_pair("XSD1", "876543210121")
    else:
        pipeline.rematch("XSD1")
    shipment = pipeline.STORE.get_shipment("XSD1")
    assert shipment["salesperson"] == "新人员"
    assert not shipment.get("salesperson_id")
    pipeline.track_update("XSD1", "异常")
    tasks = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert len(tasks) == (2 if update == "pair" else 1)
    assert all(task["payload"]["name"] == "新人员" for task in tasks)
    assert all(not task["payload"].get("uid") for task in tasks)


def test_unchanged_salesperson_keeps_verified_identity(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "876543210121", "salesperson": "已确认人员", "salesperson_id": "user-1"})
    monkeypatch.setattr(pipeline, "match_sales", lambda *args, **kwargs: {"salesperson": "已确认人员"})
    pipeline.rematch("XSD1")
    assert pipeline.STORE.get_shipment("XSD1")["salesperson_id"] == "user-1"


def test_recovered_notification_fanout_rolls_back_if_any_recipient_cannot_enqueue(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "876543210121", "salesperson": "录单人", "needs_notify": True,
        "history": [{"at": "event-1", "to": "运输中"}], "products": []})
    with pipeline.STORE.connect() as connection:
        connection.execute("""CREATE TRIGGER reject_dm BEFORE INSERT ON tasks
            WHEN NEW.kind='notify_dm' BEGIN SELECT RAISE(ABORT,'queue unavailable'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="queue unavailable"):
        pipeline._queue_notifications("channel")
    assert pipeline.STORE.list_tasks() == []
    assert pipeline.STORE.get_shipment("XSD1")["needs_notify"] is True
    with pipeline.STORE.connect() as connection:
        connection.execute("DROP TRIGGER reject_dm")
    pipeline._queue_notifications("channel")
    assert sorted(row["kind"] for row in pipeline.STORE.list_tasks()) == ["notify_dm", "notify_group"]


@pytest.mark.parametrize("reported_version", [None, 0])
@pytest.mark.parametrize("path", ["package", "legacy"])
def test_reused_tracking_rejects_result_without_binding_version(monkeypatch, tmp_path, reported_version, path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "876543210121", "history": [], "products": [], "binding_version": 1})
    pipeline.replace_package("XSD1", "876543210121", "876543210122", "ops", "new label")
    pipeline.replace_package("XSD1", "876543210122", "876543210121", "ops", "restore number")
    if path == "package":
        result = pipeline.package_update("XSD1", "876543210121", "签收", binding_version=reported_version)
    else:
        result = pipeline.track_update("XSD1", "签收", tracking="876543210121", binding_version=reported_version)
    assert result["reason"] == "stale binding"
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "已出国际单"


def test_current_binding_can_update_after_reusing_tracking(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "876543210121", "history": [], "products": [], "binding_version": 1})
    pipeline.replace_package("XSD1", "876543210121", "876543210122", "ops", "new label")
    replacement = pipeline.replace_package("XSD1", "876543210122", "876543210121", "ops", "restore number")
    result = pipeline.package_update("XSD1", "876543210121", "运输中",
                                     binding_version=replacement["binding_version"])
    assert result["changed"] is True


@pytest.mark.parametrize("reported_version", [None, 0])
def test_legacy_binding_generation_requires_version_even_without_history(monkeypatch, tmp_path, reported_version):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "876543210121", "binding_version": 3, "history": []})
    rejected = pipeline.track_update("XSD1", "签收", tracking="876543210121",
                                     binding_version=reported_version)
    assert rejected["reason"] == "stale binding"
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "运输中"
    assert not pipeline.STORE.list_tasks()
    accepted = pipeline.track_update("XSD1", "签收", tracking="876543210121", binding_version=3)
    assert accepted["changed"] is True
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "签收"
