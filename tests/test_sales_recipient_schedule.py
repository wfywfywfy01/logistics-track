from types import SimpleNamespace

import pytest

from test_reliability import load_auto_track


@pytest.mark.parametrize("sync_ok", [True, False])
def test_sales_recipients_sync_before_new_events_and_failure_stays_retryable(
    monkeypatch, tmp_path, sync_ok
):
    auto = load_auto_track(monkeypatch, tmp_path)
    monkeypatch.setenv("TRACK_STEP_TIMEOUT_SECONDS", "8000")
    monkeypatch.setenv("STEP_TIMEOUT_SECONDS", "1000")
    task_id = auto.STORE.enqueue_task("pipeline", "recipient-sync", {})
    calls, leases = [], []
    claim = auto.STORE.claim_task

    def capture_claim(*args, **kwargs):
        leases.append(kwargs["lease_seconds"])
        return claim(*args, **kwargs)

    def run(args, description):
        calls.append(args)
        return sync_ok if "sync-sales-recipients" in args else True

    monkeypatch.setattr(auto.STORE, "claim_task", capture_claim)
    monkeypatch.setattr(auto, "run", run)
    options = SimpleNamespace(
        queued_only=False, skip_track=False, mode="full",
        channel_id="channel", bot_app_id="bot",
    )

    assert auto.execute(options) == (0 if sync_ok else 1)
    sync = ["tracking-pipeline.py", "sync-sales-recipients", "--apply"]
    assert calls.index(sync) < calls.index(["wire_results.py"])
    assert min(leases) >= 8000 + 6 * 1000
    saved = next(row for row in auto.STORE.list_tasks() if row["id"] == task_id)
    assert saved["status"] == ("succeeded" if sync_ok else "retry")
