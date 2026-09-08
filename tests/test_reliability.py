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
    assert "bad workbook" in result["error"]


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

    def send_and_add_order(*args, **kwargs):
        current = json.loads(ledger.read_text(encoding="utf-8"))
        current["XSD2"] = {"status": "已预报"}
        ledger.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        return 0, '{"ok": true}', ""

    monkeypatch.setattr(robust, "cli_run", send_and_add_order)
    runpy.run_path(str(ROOT / "send_dms.py"), run_name="__main__")

    saved = json.loads(ledger.read_text(encoding="utf-8"))
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
