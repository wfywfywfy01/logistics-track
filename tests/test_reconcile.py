import sys

import reconcile
from storage import Storage


def test_dry_report_exposes_pipeline_review_and_tracking_backlog(
        monkeypatch, tmp_path, capsys):
    store = Storage(tmp_path)
    pipeline_id = store.enqueue_task("pipeline", "pipeline:failed", {})
    claimed = store.claim_task("test", kind="pipeline")
    assert claimed["id"] == pipeline_id
    store.fail_task(pipeline_id, "failed", max_attempts=1)
    store.enqueue_task("pipeline", "pipeline:waiting", {})
    store.enqueue_task("review", "review:XSD1", {"order": "XSD1"})
    store.enqueue_task("tracking_failure", "tracking-failure:XSD1:1Z1", {
        "order": "XSD1", "tracking": "1Z1"})
    store.enqueue_task("stalled", "stalled:XSD2", {"order": "XSD2", "tracking": "1Z2"})
    monkeypatch.setattr(reconcile, "Storage", lambda: store)
    monkeypatch.setattr(sys, "argv", ["reconcile.py", "--channel-id", "channel", "--dry"])

    assert reconcile.main() == 0
    output = capsys.readouterr().out
    assert "流水线待执行 1 项；失败终止 1 项" in output
    assert "人工审核 1 项" in output
    assert "官网抓取异常待办 1 项" in output
    assert "物流停滞待办 1 项" in output
