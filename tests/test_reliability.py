import importlib.util
import json
import os
import re
import runpy
import ssl
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest

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


def load_ocr_label(monkeypatch, tmp_path):
    monkeypatch.setenv("LOGIBOT_DATA_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("ocr_label_test", ROOT / "ocr_label.py")
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


def test_ocr_request_timeout_is_capped(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("ocr_label", ROOT / "ocr_label.py")
    ocr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ocr)
    image = tmp_path / "label.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    timeouts = []

    def unavailable(_request, timeout, context):
        timeouts.append(timeout)
        raise TimeoutError("provider unavailable")

    monkeypatch.setenv("OCR_REQUEST_TIMEOUT", "999")
    monkeypatch.setattr("urllib.request.urlopen", unavailable)

    with pytest.raises(TimeoutError):
        ocr.ocr_image(image)

    assert timeouts == [60]


@pytest.mark.skipif(os.name == "nt", reason="production hard deadline uses SIGALRM")
def test_ocr_request_deadline_stops_slow_stream(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("ocr_label", ROOT / "ocr_label.py")
    ocr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ocr)
    image = tmp_path / "label.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    class SlowHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OCR_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("OCR_REQUEST_TIMEOUT", "1")
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            ocr.ocr_image(image)
    finally:
        server.shutdown()
        server.server_close()
    assert time.monotonic() - started < 2


def test_watcher_processes_distinct_message_with_same_timestamp(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    timestamp = "2026-09-08T01:00:00Z"
    message = {"id": "message-2", "created_at": timestamp, "body": "XSD1==1Z1234567890"}
    monkeypatch.setattr(watcher, "cli_json", lambda args, timeout=None: {"messages": [message]})
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


def test_watcher_history_retries_same_page_before_advancing(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    timestamp = "2026-09-08T01:00:00Z"
    calls, sleeps = [], []
    replies = [RuntimeError("CLI rc=1: temporary failure"), {
        "messages": [{"id": "message-1", "created_at": timestamp, "body": "hello"}]
    }]

    def history(args, timeout=None):
        calls.append(args)
        assert 1 <= timeout <= 60
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(watcher, "cli_json", history)
    monkeypatch.setattr(watcher.time, "sleep", sleeps.append)

    assert watcher.fetch_history("channel", timestamp) == timestamp
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert sleeps == [1]


def test_watcher_history_failure_reports_cli_reason(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    monkeypatch.setattr(watcher.robust, "cli_run", lambda _args: (
        1, "", "upstream authentication unavailable\n"))

    with pytest.raises(RuntimeError, match="rc=1.*authentication unavailable"):
        watcher.cli_json(["im", "+history"])


def test_watcher_history_retries_invalid_response_schema(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    replies = [{}, {"messages": []}]
    monkeypatch.setattr(watcher, "cli_json", lambda _args, timeout=None: replies.pop(0))
    monkeypatch.setattr(watcher.time, "sleep", lambda _seconds: None)

    assert watcher.fetch_history("channel", "2026-09-08T01:00:00Z") == \
        "2026-09-08T01:00:00Z"
    assert replies == []


def test_watcher_history_round_has_hard_budget(monkeypatch, tmp_path):
    watcher = load_watcher(monkeypatch, tmp_path)
    monkeypatch.setenv("HISTORY_ATTEMPTS", "5")
    monkeypatch.setenv("HISTORY_BUDGET_SECONDS", "300")
    monkeypatch.setenv("HISTORY_REQUEST_TIMEOUT_SECONDS", "999")
    clock = iter((0, 0, 301))
    monkeypatch.setattr(watcher.time, "monotonic", lambda: next(clock))
    calls = []

    def unavailable(_args, timeout=None):
        calls.append(timeout)
        raise RuntimeError("CLI timed out")

    monkeypatch.setattr(watcher, "cli_json", unavailable)
    monkeypatch.setattr(watcher.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="budget exhausted"):
        watcher.fetch_history("channel", "2026-09-08T01:00:00Z")
    assert calls == [60]


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
    assert states == {"label-1": "succeeded", "label-2": "succeeded"}
    assert auto_track.STORE.pending_task_count("review") == 1


def test_ocr_without_complete_pair_goes_to_review_without_retry(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    image = tmp_path / "unclear.png"
    image.write_bytes(b"fixture")
    auto_track.STORE.enqueue_inbox(
        "label-unclear", {"name": "unclear.png", "path": str(image)}
    )
    result = types.SimpleNamespace(
        returncode=2,
        stdout=json.dumps({"ok": True, "pairs": [], "ingested": []}).encode(),
    )
    monkeypatch.setattr(auto_track.subprocess, "run", lambda *args, **kwargs: result)
    monkeypatch.setattr(auto_track, "run", lambda *args: True)
    options = types.SimpleNamespace(queued_only=False, skip_track=True, mode="incremental",
                                    channel_id="channel", bot_app_id="bot")

    assert auto_track.execute(options) == 0
    inbox = auto_track.STORE.get_inbox()[0]
    reviews = auto_track.STORE.list_tasks(("pending",), kinds=("review",))
    assert inbox["status"] == "succeeded"
    assert inbox["attempts"] == 1
    assert len(reviews) == 1
    assert reviews[0]["payload"]["source_inbox_id"] == "label-unclear"


def test_partial_ocr_result_queues_success_and_reuses_pair_review(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    image = tmp_path / "mixed.png"
    image.write_bytes(b"fixture")
    auto_track.STORE.enqueue_inbox("label-mixed", {"name": "mixed.png", "path": str(image)})
    auto_track.STORE.enqueue_task("review", "pair-review:XSD2:1Z2", {
        "reason": "unknown order", "order": "XSD2", "intl": "1Z2"})
    result = types.SimpleNamespace(returncode=2, stdout=json.dumps({
        "ok": False,
        "retryable": False,
        "pairs": [["XSD1", "1Z1"], ["XSD2", "1Z2"]],
        "ingested": [
            {"order": "XSD1", "intl": "1Z1", "ok": True},
            {"order": "XSD2", "intl": "1Z2", "ok": False},
        ],
    }).encode())
    monkeypatch.setattr(auto_track.subprocess, "run", lambda *args, **kwargs: result)
    monkeypatch.setattr(auto_track, "run", lambda *args: True)
    options = types.SimpleNamespace(queued_only=False, skip_track=True, mode="incremental",
                                    channel_id="channel", bot_app_id="bot")

    assert auto_track.execute(options) == 0
    assert auto_track.STORE.get_inbox()[0]["status"] == "succeeded"
    assert auto_track.STORE.pending_task_count("pipeline") == 0
    assert len(auto_track.STORE.list_tasks(("pending",), kinds=("review",))) == 1
    assert auto_track.STORE.task_counts()["pipeline"]["succeeded"] == 1


def test_ocr_cli_marks_provider_failure_as_retryable(monkeypatch, tmp_path, capsys):
    ocr = load_ocr_label(monkeypatch, tmp_path)
    image = tmp_path / "label.png"
    image.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(ocr, "ocr_image", lambda _path: calls.append(1) or (
        _ for _ in ()).throw(TimeoutError("provider timeout")))
    monkeypatch.setattr(sys, "argv", ["ocr_label.py", "--image", str(image)])

    assert ocr.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["retryable"] is True
    assert "TimeoutError" in payload["error"]
    assert len(calls) == 1


def test_ocr_cli_marks_readable_image_without_pair_for_review(monkeypatch, tmp_path, capsys):
    ocr = load_ocr_label(monkeypatch, tmp_path)
    image = tmp_path / "label.png"
    image.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(ocr, "ocr_image", lambda _path: calls.append(1) or
                        "没有可识别的订单号和运单号")
    monkeypatch.setattr(sys, "argv", ["ocr_label.py", "--image", str(image)])

    assert ocr.main() == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["retryable"] is False
    assert len(calls) == 1


def test_ocr_ingest_timeout_is_retryable_and_bounded(monkeypatch, tmp_path, capsys):
    ocr = load_ocr_label(monkeypatch, tmp_path)
    image = tmp_path / "label.png"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(ocr, "ocr_image", lambda _path: "XSD1==1Z1234567890")
    timeouts = []

    def timeout_ingest(*_args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        raise __import__("subprocess").TimeoutExpired("ingest-pair", kwargs["timeout"])

    monkeypatch.setattr(ocr.subprocess, "run", timeout_ingest)
    monkeypatch.setattr(sys, "argv", ["ocr_label.py", "--image", str(image)])

    assert ocr.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["retryable"] is True
    assert payload["ingested"][0]["ok"] is False
    assert 1 <= timeouts[0] <= 120


@pytest.mark.parametrize(("returncode", "expected_rc", "retryable"), (
    (1, 1, True),
    (2, 2, False),
))
def test_ocr_ingest_exit_code_distinguishes_retry_from_review(
        monkeypatch, tmp_path, capsys, returncode, expected_rc, retryable):
    ocr = load_ocr_label(monkeypatch, tmp_path)
    image = tmp_path / "label.png"
    image.write_bytes(b"fixture")
    monkeypatch.setattr(ocr, "ocr_image", lambda _path: "XSD1==1Z1234567890")
    result = types.SimpleNamespace(returncode=returncode, stdout=b"failure", stderr=b"")
    monkeypatch.setattr(ocr.subprocess, "run", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(sys, "argv", ["ocr_label.py", "--image", str(image)])

    assert ocr.main() == expected_rc
    assert json.loads(capsys.readouterr().out)["retryable"] is retryable


def test_failed_pipeline_task_remains_retryable(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    auto_track.STORE.enqueue_task("pipeline", "pipeline:message-1", {})
    monkeypatch.setattr(auto_track, "run", lambda *args: False)
    monkeypatch.setattr(
        sys, "argv", ["auto-track.py", "--channel-id", "channel", "--queued-only"]
    )

    assert auto_track.main() == 1
    assert auto_track.STORE.pending_task_count("pipeline") == 1


def test_sheet_sync_failure_does_not_fail_completed_pipeline(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    auto_track.STORE.enqueue_task("pipeline", "pipeline:message-1", {})
    calls = []

    def run(args, _description):
        calls.append(args[0])
        return args[0] != "sync_sheet.py"

    monkeypatch.setattr(auto_track, "run", run)
    options = types.SimpleNamespace(queued_only=True, skip_track=True, mode="incremental",
                                    channel_id="channel", bot_app_id="bot")

    assert auto_track.execute(options) == 0
    assert "sync_sheet.py" in calls
    assert auto_track.STORE.task_counts()["pipeline"]["succeeded"] == 1


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


def test_busy_auto_track_is_not_reported_as_success(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_track.robust, "FileLock", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(TimeoutError("busy")))
    monkeypatch.setattr(
        sys, "argv", ["auto-track.py", "--channel-id", "channel", "--mode", "full"])

    assert auto_track.main() == 75


def test_tracking_step_has_a_separate_batch_timeout(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    seen = {}

    def completed(*_args, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.delenv("STEP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("TRACK_STEP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(auto_track.subprocess, "run", completed)

    assert auto_track.run(["track_all_ups.py", "--mode", "full"], "抓官网") is True
    assert seen["timeout"] == 7200


def test_durable_notification_failure_does_not_repeat_carrier_batch(monkeypatch, tmp_path):
    auto_track = load_auto_track(monkeypatch, tmp_path)
    options = types.SimpleNamespace(
        channel_id="channel", bot_app_id="bot", skip_track=False,
        queued_only=False, mode="full")

    def step(args, _description):
        return args[0] != "tracking-pipeline.py" or "notify" not in args

    monkeypatch.setattr(auto_track, "run", step)

    assert auto_track.execute(options) == 0


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
        "history": [], "products": [], "binding_version": 1,
        "packages": [{"tracking": "1Z999AA10123456784", "carrier": "UPS",
                      "role": "primary", "status": "运输中", "binding_version": 1,
                      "history": [], "binding_history": []}]})
    monkeypatch.setattr(pipeline, "match_sales", lambda *_args, **_kwargs: {})

    result = pipeline.ingest_pair("XSD1", "876543210123", force=True)

    assert result["paired"] == "XSD1"
    saved = pipeline.STORE.get_shipment("XSD1")
    assert saved["carrier"] == "FEDEX"
    assert saved["packages"][0]["tracking"] == "876543210123"
    assert saved["packages"][0]["carrier"] == "FEDEX"
    assert saved["packages"][0]["binding_history"][-1]["from"] == "1Z999AA10123456784"
    assert saved["packages"][0]["binding_history"][-1]["to"] == "876543210123"
    assert saved["packages"][0]["binding_history"][-1]["snapshot"]["tracking"] == "1Z999AA10123456784"
    assert saved["packages"][0]["binding_version"] == 2
    assert saved["binding_version"] == 2
