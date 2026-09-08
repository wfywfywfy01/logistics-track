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
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    ledger = data / "shipments.json"
    ledger.write_text(
        json.dumps(
            {"XSD1": {"salesperson": "张三", "status": "运输中", "intl": "1Z1"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data / "ups_results.json").write_text(
        json.dumps({"XSD1": {"ok": True}}, ensure_ascii=False), encoding="utf-8"
    )
    (data / "users_map.json").write_text(
        json.dumps({"张三": "user-1"}, ensure_ascii=False), encoding="utf-8"
    )

    import robust
    from storage import Storage

    def send_and_add_order(*args, **kwargs):
        Storage(data).upsert_shipment("XSD2", {"orderNo": "XSD2", "status": "已预报"})
        return 0, '{"ok": true}', ""

    monkeypatch.setattr(robust, "cli_run", send_and_add_order)
    runpy.run_path(str(ROOT / "send_dms.py"), run_name="__main__")

    saved = Storage(data).get_shipments()
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
