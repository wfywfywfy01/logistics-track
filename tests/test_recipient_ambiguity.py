import pytest

from test_reliability import load_pipeline


def prepare(monkeypatch, tmp_path, state="unknown", old_uid=None, org=None):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    shipment = {"orderNo": "XSD1", "salesperson": "同名人员", "sales_recipient_ids": [1, 2],
                "status": "签收", "intl": "1Z1", "history": [{"at": "event-1", "to": "签收"}]}
    pipeline.STORE.upsert_shipment("XSD1", shipment)
    pipeline.STORE.put_document("org_people", org if org is not None else {"同名人员": [1, 2]})
    old = pipeline.STORE.enqueue_task("notify_dm", "dm:XSD1:event-1", {
        "order": "XSD1", "event_key": "event-1", "name": "同名人员", "uid": old_uid})
    if state == "unknown":
        pipeline.STORE.mark_task_unknown(old, "receipt missing")
    elif state == "succeeded":
        pipeline.STORE.complete_task(old)
    return pipeline, shipment, old


@pytest.mark.parametrize("state", ["unknown", "succeeded", "pending"])
def test_unresolved_legacy_name_holds_new_uid_notifications(monkeypatch, tmp_path, state):
    pipeline, shipment, old = prepare(monkeypatch, tmp_path, state)
    tasks = pipeline._event_notification_tasks("XSD1", shipment, shipment["history"][-1])
    assert not [task for task in tasks if task["kind"] == "notify_dm" and task["payload"].get("uid")]
    review = next(task for task in tasks if task["kind"] == "review")
    assert review["dedupe_key"] == "recipient-review:XSD1:event-1"
    assert review["payload"]["reason"] == "legacy recipient identity unresolved"
    assert pipeline.STORE.list_tasks()[0]["id"] == old
    assert pipeline.STORE.list_tasks()[0]["status"] == state


def test_ambiguous_directory_cannot_be_bypassed_by_primary_id(monkeypatch, tmp_path):
    pipeline, shipment, _ = prepare(monkeypatch, tmp_path)
    shipment["salesperson_id"] = 1
    tasks = pipeline._event_notification_tasks("XSD1", shipment, shipment["history"][-1])
    assert not [task for task in tasks if task["kind"] == "notify_dm"]
    assert any(task["kind"] == "review" for task in tasks)


@pytest.mark.parametrize("org", [{}, {"同名人员": [1]}])
def test_confirmed_primary_identity_does_not_hold_other_recipient(monkeypatch, tmp_path, org):
    pipeline, shipment, _ = prepare(monkeypatch, tmp_path, org=org)
    shipment["salesperson_id"] = 1
    tasks = pipeline._event_notification_tasks("XSD1", shipment, shipment["history"][-1])
    assert not any(task["kind"] == "review" for task in tasks)
    assert any(task["kind"] == "notify_dm" and task["payload"].get("uid") == 2 for task in tasks)


@pytest.mark.parametrize("order,event", [("OTHER", "event-1"), ("XSD1", "event-2")])
def test_other_order_or_event_does_not_block_recipients(monkeypatch, tmp_path, order, event):
    pipeline, shipment, _ = prepare(monkeypatch, tmp_path)
    tasks = pipeline._event_notification_tasks(order, shipment, {"at": event, "to": "签收"})
    assert not any(task["kind"] == "review" for task in tasks)
    assert {task["payload"]["uid"] for task in tasks if task["kind"] == "notify_dm"} == {1, 2}


@pytest.mark.parametrize("state", ["unknown", "succeeded"])
def test_known_old_uid_deduplicates_without_directory_or_legacy_key(monkeypatch, tmp_path, state):
    pipeline, shipment, old = prepare(monkeypatch, tmp_path, state, old_uid=1, org={})
    tasks = pipeline._event_notification_tasks("XSD1", shipment, shipment["history"][-1])
    pipeline.STORE.mutate_shipment("XSD1", lambda current: (current, tasks))
    direct = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert len(direct) == 2
    assert next(task for task in direct if task["id"] == old)["status"] == state
    assert [task["payload"]["uid"] for task in direct if task["status"] == "pending"] == [2]


def test_resolved_legacy_uid_does_not_depend_on_new_display_name(monkeypatch, tmp_path):
    pipeline, shipment, old = prepare(monkeypatch, tmp_path, org={"同名人员": [1], "另一别名": [1]})
    shipment["salesperson"] = "当前主要人员"
    tasks = pipeline._event_notification_tasks("XSD1", shipment, shipment["history"][-1])
    pipeline.STORE.mutate_shipment("XSD1", lambda current: (current, tasks))
    direct = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert len(direct) == 2
    assert next(task for task in direct if task["id"] == old)["status"] == "unknown"
    assert [task["payload"]["uid"] for task in direct if task["status"] == "pending"] == [2]
