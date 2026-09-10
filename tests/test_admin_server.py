import base64
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from admin_server import create_server, csrf_token
from storage import Storage


def request(url, token=None, method="GET", payload=None, csrf=None):
    headers = {}
    if token:
        raw = base64.b64encode(("admin:" + token).encode()).decode()
        headers["Authorization"] = "Basic " + raw
    if csrf:
        headers["X-CSRF-Token"] = csrf
    data = None if payload is None else json.dumps(payload).encode()
    if data:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers, method=method)) as response:
        return response.status, json.loads(response.read())


def run_server(tmp_path):
    store = Storage(tmp_path)
    server = create_server(store, "secret-token", "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return store, server, "http://127.0.0.1:%d" % server.server_port


def test_admin_api_requires_auth_and_exposes_order_detail(tmp_path):
    store, server, base = run_server(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "intl": "1Z1", "salesperson": "张三", "history": [{"to": "运输中"}]})
    try:
        try:
            request(base + "/api/orders")
            assert False, "missing auth must fail"
        except HTTPError as error:
            assert error.code == 401
        status, body = request(base + "/api/orders?q=XSD1", "secret-token")
        assert status == 200 and body["items"][0]["orderNo"] == "XSD1"
        _, detail = request(base + "/api/orders/XSD1", "secret-token")
        assert detail["shipment"]["history"] == [{"to": "运输中"}]
    finally:
        server.shutdown()


def test_task_actions_require_csrf_and_write_audit_log(tmp_path):
    store, server, base = run_server(tmp_path)
    task_id = store.enqueue_task("review", "review:XSD1", {"order": "XSD1"})
    task = store.claim_task("worker", kind="review")
    store.fail_task(task["id"], "needs review", max_attempts=1)
    try:
        try:
            request(base + f"/api/tasks/{task_id}/retry", "secret-token", "POST",
                    {"operator": "ops", "reason": "checked"})
            assert False, "missing csrf must fail"
        except HTTPError as error:
            assert error.code == 403
        status, body = request(base + f"/api/tasks/{task_id}/retry", "secret-token",
                               "POST", {"operator": "ops", "reason": "checked"},
                               csrf_token("secret-token"))
        assert status == 200 and body["status"] == "pending"
        assert store.pending_task_count("review") == 1
        assert store.list_audit("XSD1")[0]["operator"] == "ops"
    finally:
        server.shutdown()


def test_issue_list_includes_dead_ocr_and_incoming_messages(tmp_path):
    store, server, base = run_server(tmp_path)
    store.enqueue_inbox("label-1", {"order": "XSD1", "path": "label.png"})
    claimed = store.claim_inbox("worker")
    store.fail_inbox(claimed["id"], "ocr failed", max_attempts=1)
    store.record_message("channel", "message-1", "2026-09-10T00:00:00Z", {"order": "XSD1"})
    store.fail_message("channel", "message-1", "parse failed", max_attempts=1)
    try:
        _, body = request(base + "/api/tasks", "secret-token")
        assert {item["source"] for item in body["items"]} == {"inbox", "incoming_message"}
    finally:
        server.shutdown()


def test_operator_can_retry_ocr_and_assign_stable_salesperson_id(tmp_path):
    store, server, base = run_server(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报"})
    store.enqueue_inbox("label-1", {"order": "XSD1", "path": "label.png"})
    claimed = store.claim_inbox("worker"); store.fail_inbox(claimed["id"], "ocr", max_attempts=1)
    auth_csrf = csrf_token("secret-token")
    try:
        _, retry = request(base + "/api/inbox/label-1/retry", "secret-token", "POST",
                           {"operator": "ops", "reason": "image repaired"}, auth_csrf)
        _, assigned = request(base + "/api/orders/XSD1/salesperson", "secret-token", "POST",
                              {"operator": "ops", "reason": "verified in HR",
                               "salesperson": "张三", "user_id": "user-1"}, auth_csrf)
        assert retry["status"] == "pending"
        assert assigned["salesperson_id"] == "user-1"
        assert store.get_shipment("XSD1")["salesperson_id"] == "user-1"
        assert len(store.list_audit("XSD1")) == 2
    finally:
        server.shutdown()


def test_operator_can_add_package_through_admin_api(tmp_path):
    store, server, base = run_server(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
                                    "history": [], "products": []})
    try:
        _, result = request(base + "/api/orders/XSD1/packages", "secret-token", "POST",
                            {"operator": "ops", "reason": "verified label",
                             "tracking": "876543210123", "carrier": "FedEx"},
                            csrf_token("secret-token"))
        assert result["added"] is True
        assert store.get_shipment("XSD1")["packages"][0]["carrier"] == "FEDEX"
    finally:
        server.shutdown()


def test_admin_cannot_retry_task_during_delivery(tmp_path):
    store, server, base = run_server(tmp_path)
    task_id = store.enqueue_task("notify_group", "group:XSD1:event-1",
                                 {"order": "XSD1", "event_key": "event-1"})
    task = store.claim_task("sender", lease_seconds=120, kind="notify_group")
    store.mark_delivery_inflight(task["id"])
    try:
        try:
            request(base + f"/api/tasks/{task_id}/retry", "secret-token", "POST",
                    {"operator": "ops", "reason": "manual retry"}, csrf_token("secret-token"))
            assert False, "active delivery must reject retry"
        except HTTPError as error:
            assert error.code == 409
        assert store.list_tasks(kinds=("notify_group",))[0]["status"] == "unknown"
    finally:
        server.shutdown()
