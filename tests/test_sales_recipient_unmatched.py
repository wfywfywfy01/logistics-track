import sys

import pytest

from test_reliability import load_pipeline


EMPTY = {"rows": [], "pagination": {"has_more": False, "total_rows": 0}}


def prepare(monkeypatch, tmp_path, response=EMPTY):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "salesperson": "原人员",
        "sales_recipient_ids": [99], "domestic": "", "intl": "1Z1",
        "history": [{"at": "old-event", "to": "运输中"}], "needs_notify": False})
    monkeypatch.setattr(pipeline, "cli_json", lambda args: response)
    monkeypatch.setattr(sys, "argv", ["tracking-pipeline.py", "sync-sales-recipients", "--apply"])
    return pipeline


def test_complete_unmatched_batch_creates_one_review_and_keeps_pipeline_success(monkeypatch, tmp_path):
    pipeline = prepare(monkeypatch, tmp_path)
    before = pipeline.STORE.get_shipment("XSD1")
    assert pipeline.main() == 0
    assert pipeline.main() == 0
    reviews = pipeline.STORE.list_tasks(kinds=("review",))
    assert len(reviews) == 1
    assert reviews[0]["dedupe_key"] == "sales-recipients-review:XSD1"
    assert reviews[0]["status"] == "pending"
    assert pipeline.STORE.get_shipment("XSD1") == before
    assert not pipeline.STORE.list_tasks(kinds=("notify_group", "notify_dm"))


def test_explicit_unmatched_order_remains_a_failed_command(monkeypatch, tmp_path):
    pipeline = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--order", "XSD1"])
    assert pipeline.main() == 1
    assert len(pipeline.STORE.list_tasks(kinds=("review",))) == 1


@pytest.mark.parametrize("response", [None, [], "unavailable", {}, {"rows": None}, {"ok": False, "rows": []}])
def test_upstream_failure_remains_blocking_and_preserves_verified_ids(monkeypatch, tmp_path, response):
    pipeline = prepare(monkeypatch, tmp_path, response)
    before = pipeline.STORE.get_shipment("XSD1")
    assert pipeline.main() == 1
    assert pipeline.STORE.get_shipment("XSD1") == before
    assert not pipeline.STORE.list_tasks()


def test_recovery_closes_only_recipient_review_without_replaying_old_event(monkeypatch, tmp_path):
    pipeline = prepare(monkeypatch, tmp_path)
    assert pipeline.main() == 0
    unrelated = pipeline.STORE.enqueue_task("review", "pair-review:XSD1:other", {"order": "XSD1"})
    monkeypatch.setattr(pipeline, "cli_json", lambda args: {
        "rows": [{"订单号": "XSD1", "销售人员": "原人员", "销售user_id": "99、12537"}],
        "pagination": {"has_more": False, "total_rows": 1}})
    assert pipeline.main() == 0
    reviews = {task["dedupe_key"]: task for task in pipeline.STORE.list_tasks(kinds=("review",))}
    assert reviews["sales-recipients-review:XSD1"]["status"] == "succeeded"
    assert reviews["pair-review:XSD1:other"]["id"] == unrelated
    assert reviews["pair-review:XSD1:other"]["status"] == "pending"
    assert pipeline.STORE.get_shipment("XSD1")["sales_recipient_ids"] == [99, 12537]
    assert not pipeline.STORE.list_tasks(kinds=("notify_group", "notify_dm"))


def test_dry_unmatched_sync_does_not_create_review(monkeypatch, tmp_path):
    pipeline = prepare(monkeypatch, tmp_path)
    before = pipeline.STORE.get_shipment("XSD1")
    result = pipeline.sync_sales_recipients()
    assert result["failed"] == 1
    assert pipeline.STORE.get_shipment("XSD1") == before
    assert not pipeline.STORE.list_tasks()


def test_complete_response_without_uid_creates_review(monkeypatch, tmp_path):
    pipeline = prepare(monkeypatch, tmp_path, {
        "rows": [{"订单号": "XSD1", "销售人员": "原人员"}],
        "pagination": {"has_more": False, "total_rows": 1}})
    assert pipeline.main() == 0
    assert len(pipeline.STORE.list_tasks(kinds=("review",))) == 1
    assert pipeline.STORE.get_shipment("XSD1")["sales_recipient_ids"] == [99]


def test_unmatched_order_does_not_prevent_other_order_sync(monkeypatch, tmp_path):
    pipeline = prepare(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD2", {"orderNo": "XSD2", "status": "运输中", "history": []})
    monkeypatch.setattr(pipeline, "cli_json", lambda args: EMPTY if args[args.index("--order-no") + 1] == "XSD1" else {
        "rows": [{"订单号": "XSD2", "销售人员": "新人员", "销售user_id": "12537"}],
        "pagination": {"has_more": False, "total_rows": 1}})
    assert pipeline.main() == 0
    assert pipeline.STORE.get_shipment("XSD2")["sales_recipient_ids"] == [12537]
