import importlib.util
import json
import re
import runpy
import ssl
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_pipeline(monkeypatch, tmp_path):
    monkeypatch.setenv("LOGIBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "openpyxl", types.ModuleType("openpyxl"))
    spec = importlib.util.spec_from_file_location(
        "tracking_pipeline", ROOT / "tracking-pipeline.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_watcher(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("logi_watcher", ROOT / "logi-watcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_auto_track(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location("auto_track", ROOT / "auto-track.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pipeline_recovers_corrupt_ledger_from_backup(monkeypatch, tmp_path):
    ledger = tmp_path / "shipments.json"
    ledger.write_text("{broken", encoding="utf-8")
    Path(str(ledger) + ".bak").write_text(
        json.dumps({"XSD1": {"status": "运输中"}}), encoding="utf-8"
    )

    pipeline = load_pipeline(monkeypatch, tmp_path)

    assert pipeline.load_json(ledger) == {"XSD1": {"status": "运输中"}}


def test_failed_forecast_import_is_not_acknowledged(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    attachment = {
        "name": "broken.xlsx",
        "url": "https://example.invalid/test-file-123456",
    }
    uid = re.sub(r"[^A-Za-z0-9-]", "", attachment["url"])[-12:]
    (tmp_path / "tmp" / f"{uid}_broken.xlsx").write_bytes(b"invalid")
    failed = types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad workbook")
    monkeypatch.setattr(watcher.subprocess, "run", lambda *args, **kwargs: failed)

    result = watcher.process_attachment(attachment, "channel", "bot")

    assert result["ok"] is False
    assert result["error"] == "invalid xlsx content"


def test_dm_checkpoint_does_not_overwrite_concurrent_ledger_change(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "salesperson": "张三", "status": "运输中",
        "intl": "1Z1", "needs_notify": True, "binding_version": 1,
        "history": [{"from": "已出国际单", "to": "运输中", "at": "event-1"}],
        "products": [],
    })
    pipeline.STORE.put_document("org_people", {"张三": "user-1"})

    def send_and_add_order(_args):
        pipeline.STORE.upsert_shipment("XSD2", {"orderNo": "XSD2", "status": "已预报"})
        return "ok"

    monkeypatch.setattr(pipeline, "cli", send_and_add_order)
    pipeline.notify("channel")

    saved = pipeline.STORE.get_shipments()
    assert "XSD2" in saved
    assert saved["XSD1"]["dm_notified_status"] == "运输中"


def test_ocr_api_verifies_tls_certificate(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("ocr_label", ROOT / "ocr_label.py")
    ocr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ocr)
    image = tmp_path / "label.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    seen = {}

    class Response:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "XSD1==1Z1234567890"}}]}
            ).encode()

    def urlopen(request, timeout, context):
        seen["context"] = context
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert ocr.ocr_image(image) == "XSD1==1Z1234567890"
    assert seen["context"].check_hostname is True
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


def test_watcher_processes_distinct_message_with_same_timestamp(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    timestamp = "2026-09-08T01:00:00Z"
    message = {"id": "message-2", "created_at": timestamp, "body": "XSD1==1Z1234567890"}
    monkeypatch.setattr(watcher, "cli_json", lambda args: {"messages": [message]})
    processed = []
    monkeypatch.setattr(
        watcher,
        "process_text",
        lambda body, channel: processed.append(body)
        or [{"kind": "pair", "ok": True, "order": "XSD1"}],
    )
    monkeypatch.setattr(watcher, "spawn_auto_track", lambda *args, **kwargs: None)

    watcher.watch_once("channel", "bot", timestamp)

    assert processed == ["XSD1==1Z1234567890"]


def test_ocr_worker_does_not_overwrite_new_inbox_item(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    image = tmp_path / "first.png"
    image.write_bytes(b"fixture")
    auto_track.STORE.enqueue_inbox("label-1", {"name": "first.png", "path": str(image)})
    calls = []

    def ocr(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            auto_track.STORE.enqueue_inbox(
                "label-2", {"name": "second.png", "path": "missing.png"}
            )
            payload = {"ok": True, "pairs": [["XSD1", "1Z1234567890"]],
                       "ingested": [{"order": "XSD1", "ok": True}]}
        else:
            payload = {"ok": True, "pairs": [], "ingested": []}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())

    monkeypatch.setattr(auto_track.subprocess, "run", ocr)
    monkeypatch.setattr(auto_track, "run", lambda *args: True)
    monkeypatch.setattr(
        sys, "argv", ["auto-track.py", "--channel-id", "channel", "--skip-track"]
    )

    assert auto_track.main() == 0
    states = {item["id"]: item["status"] for item in auto_track.STORE.get_inbox()}
    assert states == {"label-1": "succeeded", "label-2": "retry"}


def test_failed_pipeline_task_remains_retryable(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    auto_track.STORE.enqueue_task("pipeline", "pipeline:message-1", {})
    monkeypatch.setattr(auto_track, "run", lambda *args: False)
    monkeypatch.setattr(
        sys, "argv", ["auto-track.py", "--channel-id", "channel", "--queued-only"]
    )

    assert auto_track.main() == 1
    assert auto_track.STORE.pending_task_count("pipeline") == 1


def test_duplicate_forecast_preserves_binding_and_queues_conflict_review(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    existing = {
        "orderNo": "XSD1", "intl": "1ZOLD", "carrier": "UPS", "binding_version": 3,
        "binding_history": [{"from": None, "to": "1ZOLD"}],
        "status": "运输中", "history": [{"from": "已出国际单", "to": "运输中"}],
    }
    pipeline.STORE.upsert_shipment("XSD1", existing)
    monkeypatch.setattr(pipeline, "parse_forecast", lambda _path: [{
        "orderNo": "XSD1", "intl": "876543210123", "products": ["new"],
        "domestic": "SF2", "carrier": "FedEx", "recipient": "buyer", "note": "new",
    }])
    monkeypatch.setattr(pipeline, "match_sales", lambda *_args, **_kwargs: {})

    pipeline.ingest_forecast("forecast.xlsx")

    saved = pipeline.STORE.get_shipment("XSD1")
    assert saved["intl"] == "1ZOLD"
    assert saved["carrier"] == "UPS"
    assert saved["binding_version"] == 3
    assert saved["binding_history"] == existing["binding_history"]
    assert saved["status"] == "运输中"
    assert pipeline.STORE.pending_task_count("review") == 1


def test_old_group_receipt_does_not_clear_new_notification(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "intl": "1Z1",
        "binding_version": 1, "needs_notify": True,
        "history": [{"from": "已出国际单", "to": "运输中", "at": "event-1"}],
        "products": [],
    })

    def send_then_advance(_args):
        pipeline.track_update("XSD1", "清关中", observed_at="2026-09-10T12:00:00+00:00")
        return "ok"

    monkeypatch.setattr(pipeline, "cli", send_then_advance)
    pipeline.notify("channel")

    saved = pipeline.STORE.get_shipment("XSD1")
    assert saved["status"] == "清关中"
    assert saved["needs_notify"] is True


def test_full_ledger_snapshot_cannot_overwrite_committed_patch(tmp_path):
    from storage import Storage
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中"})
    stale = store.get_shipments()
    store.patch_shipment("XSD1", {"notified_status": "运输中"})

    store.put_shipments(stale)

    assert store.get_shipment("XSD1")["notified_status"] == "运输中"


def test_ambiguous_org_name_invalidates_cached_user(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.put_document("users_map", {"张三": "old-user"})
    pipeline.STORE.put_document("org_people", {"张三": ["user-1", "user-2"]})
    monkeypatch.setattr(pipeline, "cli_json", lambda _args: None)

    assert pipeline.resolve_user("张三") is None
    assert "张三" not in pipeline.STORE.get_document("users_map", {})


def test_departed_person_is_not_resolved_from_stale_cache(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.put_document("users_map", {"张三": "old-user"})
    pipeline.STORE.put_document("org_people", {"李四": "user-2"})
    monkeypatch.setattr(pipeline, "cli_json", lambda _args: None)

    assert pipeline.resolve_user("张三") is None


def test_notification_only_queue_is_drained(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    auto_track.STORE.enqueue_task("notify_group", "group:XSD1:event-1", {})
    calls = []
    monkeypatch.setattr(auto_track, "run", lambda args, _desc: calls.append(args) or True)
    options = types.SimpleNamespace(queued_only=True, skip_track=False, mode="full",
                                    channel_id="channel", bot_app_id="bot")

    assert auto_track.execute(options) == 0
    assert calls == [["tracking-pipeline.py", "notify", "--channel-id", "channel",
                      "--bot-app-id", "bot"]]


def test_message_completion_and_pipeline_task_are_atomic(tmp_path):
    from storage import Storage
    store = Storage(tmp_path)
    store.record_message("channel", "message-1", "2026-09-10T00:00:00Z", {})

    store.complete_message_with_task("channel", "message-1", "pipeline",
                                     "pipeline:message-1", {"channel_id": "channel"})

    assert store.pending_messages("channel") == []
    assert store.pending_task_count("pipeline") == 1


def test_forced_rebinding_updates_carrier(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1",
        "intl": "1Z999AA10123456784", "carrier": "UPS", "status": "运输中",
        "history": [], "products": []})
    monkeypatch.setattr(pipeline, "match_sales", lambda *_args, **_kwargs: {})

    result = pipeline.ingest_pair("XSD1", "876543210123", force=True)

    assert result["paired"] == "XSD1"
    assert pipeline.STORE.get_shipment("XSD1")["carrier"] == "FEDEX"
