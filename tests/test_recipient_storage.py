from datetime import UTC, datetime, timedelta

import pytest

from storage import Storage, TaskConflict


def payload(name="何川", uid=None):
    return {"order": "XSD1", "event_key": "event-1", "name": name, "uid": uid}


@pytest.mark.parametrize("state", ["succeeded", "unknown"])
@pytest.mark.parametrize("identity", ["name", "uid"])
def test_recipient_alias_keeps_existing_delivery_state(tmp_path, state, identity):
    store = Storage(tmp_path)
    old_payload = payload(uid=12537 if identity == "uid" else None)
    old_id = store.enqueue_task("notify_dm", "old-key", old_payload)
    if state == "succeeded":
        store.complete_task(old_id)
    else:
        store.mark_task_unknown(old_id, "receipt missing")
    new_payload = payload(name="" if identity == "uid" else "何川", uid=12537)

    found_id = store.enqueue_task("notify_dm", "dm:XSD1:event-1:uid:12537", new_payload,
                                 legacy_dedupe_keys=["absent-key", "old-key"])

    assert found_id == old_id
    assert len(store.list_tasks()) == 1
    assert store.list_tasks()[0]["status"] == state


@pytest.mark.parametrize("old,new", [
    (payload(name="刘佳鑫"), payload(uid=12537)),
    (payload(name="", uid=None), payload(name="", uid=12537)),
    (payload(uid=12549), payload(uid=12537)),
])
def test_recipient_alias_does_not_suppress_another_identity(tmp_path, old, new):
    store = Storage(tmp_path)
    old_id = store.enqueue_task("notify_dm", "old-key", old)
    new_id = store.enqueue_task("notify_dm", "dm:XSD1:event-1:uid:12537", new,
                                legacy_dedupe_keys=["old-key"])
    assert new_id != old_id
    assert len(store.list_tasks()) == 2


def test_old_singular_recipient_alias_remains_supported(tmp_path):
    store = Storage(tmp_path)
    old_id = store.enqueue_task("notify_dm", "old-key", payload())
    new_id = store.enqueue_task("notify_dm", "new-key", payload(), legacy_dedupe_key="old-key")
    assert new_id == old_id


@pytest.mark.parametrize("canonical_state", ["pending", "unknown", "succeeded"])
def test_binding_name_task_cannot_repeat_existing_uid_task(tmp_path, canonical_state):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"salesperson": "何川", "needs_notify": True})
    name_id = store.enqueue_task("notify_dm", "name-key", payload())
    uid_id = store.enqueue_task("notify_dm", "dm:XSD1:event-1:uid:12537", payload(uid=12537))
    if canonical_state == "unknown":
        store.mark_task_unknown(uid_id, "receipt missing")
    elif canonical_state == "succeeded":
        store.complete_task(uid_id)
    claim = store.claim_task("worker", kind="notify_dm")
    assert claim["id"] == name_id

    assert store.bind_notification_recipient(name_id, 12537, claim=claim) is False

    tasks = {task["id"]: task for task in store.list_tasks()}
    assert tasks[uid_id]["status"] == canonical_state
    assert tasks[name_id]["payload"]["duplicate_of"] == uid_id
    assert tasks[name_id]["payload"]["uid"] == 12537
    assert "receipt" not in tasks[name_id]["payload"]
    assert store.get_shipment("XSD1")["needs_notify"] is True
    assert store.list_audit("XSD1")[0]["action"] == "deduplicated"


def test_binding_two_name_tasks_reserves_only_one_delivery(tmp_path):
    store = Storage(tmp_path)
    first_id = store.enqueue_task("notify_dm", "old-name", payload())
    second_id = store.enqueue_task("notify_dm", "new-name", payload(name="姓名别名"))
    first = store.claim_task("worker-a", kind="notify_dm")
    second = store.claim_task("worker-b", kind="notify_dm")

    assert store.bind_notification_recipient(first_id, 12537, claim=first) is True
    store.mark_delivery_inflight(first_id, claim=first)
    assert store.bind_notification_recipient(second_id, "12537", claim=second) is False

    tasks = {task["id"]: task for task in store.list_tasks()}
    assert tasks[first_id]["status"] == "unknown"
    assert tasks[first_id]["dedupe_key"] == "dm:XSD1:event-1:uid:12537"
    assert tasks[second_id]["payload"]["duplicate_of"] == first_id


@pytest.mark.parametrize("state", ["unknown", "succeeded"])
def test_binding_cannot_bypass_legacy_uid_receipt_under_another_key(tmp_path, state):
    store = Storage(tmp_path)
    old_id = store.enqueue_task("notify_dm", "legacy-key", payload(uid=12537))
    if state == "unknown":
        store.mark_task_unknown(old_id, "receipt missing")
    else:
        store.complete_task(old_id)
    # Model tasks queued by an older version before semantic UID dedupe existed.
    with store.connect() as connection:
        connection.execute("""INSERT INTO tasks
            (kind,dedupe_key,payload,next_attempt_at,created_at,updated_at)
            SELECT kind,'name-key',json_set(payload,'$.uid',NULL),next_attempt_at,created_at,updated_at
            FROM tasks WHERE id=?""", (old_id,))
    claim = store.claim_task("worker", kind="notify_dm")
    assert store.bind_notification_recipient(claim["id"], 12537, claim=claim) is False
    old = next(row for row in store.list_tasks() if row["id"] == old_id)
    assert old["status"] == state


def test_canonical_duplicate_releases_key_for_legacy_winner(tmp_path):
    store = Storage(tmp_path)
    first_id = store.enqueue_task("notify_dm", "dm:XSD1:event-1:uid:12537", payload(uid=12537))
    with store.connect() as connection:
        connection.execute("""INSERT INTO tasks
            (kind,dedupe_key,payload,next_attempt_at,created_at,updated_at)
            SELECT kind,'legacy-key',payload,next_attempt_at,created_at,updated_at
            FROM tasks WHERE id=?""", (first_id,))
    first = store.claim_task("worker-a", kind="notify_dm")
    assert first["id"] == first_id
    assert store.bind_notification_recipient(first["id"], 12537, claim=first) is False

    second = store.claim_task("worker-b", kind="notify_dm")
    assert store.bind_notification_recipient(second["id"], 12537, claim=second) is True
    store.mark_delivery_inflight(second["id"], claim=second)

    tasks = {task["id"]: task for task in store.list_tasks()}
    assert tasks[first_id]["payload"]["duplicate_of"] == second["id"]
    assert tasks[second["id"]]["dedupe_key"] == "dm:XSD1:event-1:uid:12537"
    assert tasks[second["id"]]["status"] == "unknown"


def test_expired_worker_cannot_bind_recipient(tmp_path):
    store = Storage(tmp_path)
    now = datetime.now(UTC) - timedelta(seconds=10)
    task_id = store.enqueue_task("notify_dm", "name-key", payload(), now=now)
    claim = store.claim_task("worker", now=now, lease_seconds=1, kind="notify_dm")
    with pytest.raises(TaskConflict):
        store.bind_notification_recipient(task_id, 12537, claim=claim)
    assert store.list_tasks()[0]["dedupe_key"] == "name-key"


@pytest.mark.parametrize("kind,data,uid", [
    ("notify_group", payload(), 12537),
    ("notify_dm", {"order": "XSD1"}, 12537),
    ("notify_dm", {"event_key": "event-1"}, 12537),
    ("notify_dm", payload(), ""),
    ("notify_dm", payload(), None),
    ("notify_dm", payload(), True),
])
def test_binding_rejects_incomplete_recipient_task(tmp_path, kind, data, uid):
    store = Storage(tmp_path)
    task_id = store.enqueue_task(kind, "name-key", data)
    claim = store.claim_task("worker")
    with pytest.raises(ValueError):
        store.bind_notification_recipient(task_id, uid, claim=claim)
    assert store.list_tasks()[0]["dedupe_key"] == "name-key"
