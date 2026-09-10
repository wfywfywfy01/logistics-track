import base64
import json
import re
import sqlite3
import threading
from http.cookiejar import CookieJar
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from admin_server import create_server, csrf_token
from storage import Storage


def request(url, token=None, method="GET", payload=None, csrf=None, username="admin"):
    headers = {}
    if token:
        raw = base64.b64encode((username + ":" + token).encode()).decode()
        headers["Authorization"] = "Basic " + raw
    if csrf:
        headers["X-CSRF-Token"] = csrf
    data = None if payload is None else json.dumps(payload).encode()
    if data:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers, method=method)) as response:
        return response.status, json.loads(response.read())


def request_text(url, token, username="admin"):
    raw = base64.b64encode((username + ":" + token).encode()).decode()
    with urlopen(Request(url, headers={"Authorization": "Basic " + raw})) as response:
        return response.status, response.read().decode("utf-8")


def request_bytes(url, token):
    raw = base64.b64encode(("admin:" + token).encode()).decode()
    with urlopen(Request(url, headers={"Authorization": "Basic " + raw})) as response:
        return response.status, response.headers.get_content_type(), response.read()


def run_server(tmp_path):
    store = Storage(tmp_path)
    server = create_server(store, "secret-token", "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return store, server, "http://127.0.0.1:%d" % server.server_port


def session_browser():
    jar = CookieJar()
    return build_opener(HTTPCookieProcessor(jar)), jar


def login(browser, base, username, password):
    page = browser.open(base + "/login").read().decode()
    csrf = re.search(r"name=csrf value='([^']+)'", page).group(1)
    data = urlencode({"username": username, "password": password, "csrf": csrf}).encode()
    return browser.open(Request(base + "/login", data=data, method="POST"))


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


def test_health_endpoint_is_public_and_checks_database(tmp_path):
    _, server, base = run_server(tmp_path)
    try:
        with urlopen(base + "/healthz") as response:
            result = json.loads(response.read())
        assert response.status == 200
        assert result == {"ok": True, "database": "ok"}
    finally:
        server.shutdown()


def test_health_endpoint_fails_when_database_is_missing(tmp_path):
    store, server, base = run_server(tmp_path)
    store.path = tmp_path / "missing.db"
    try:
        try:
            urlopen(base + "/healthz")
            assert False, "missing database must fail health check"
        except HTTPError as error:
            assert error.code == 503
            assert json.loads(error.read()) == {"ok": False, "database": "error"}
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
                               server.csrf_token)
        assert status == 200 and body["status"] == "pending"
        assert store.pending_task_count("review") == 1
        assert store.list_audit("XSD1")[0]["operator"] == "admin"
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
    auth_csrf = server.csrf_token
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
                            server.csrf_token)
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
                    {"operator": "ops", "reason": "manual retry"}, server.csrf_token)
            assert False, "active delivery must reject retry"
        except HTTPError as error:
            assert error.code == 409
        assert store.list_tasks(kinds=("notify_group",))[0]["status"] == "unknown"
    finally:
        server.shutdown()


def test_workbench_pages_expose_order_and_issue_actions(tmp_path):
    store, server, base = run_server(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "packages": [{"tracking": "1Z1", "carrier": "UPS"}]})
    task_id = store.enqueue_task("review", "review:XSD1", {"order": "XSD1"})
    try:
        _, order_page = request_text(base + "/orders/XSD1", "secret-token")
        _, task_page = request_text(base + "/tasks", "secret-token")
        assert "/api/orders/XSD1/salesperson" in order_page
        assert "/api/orders/XSD1/packages" in order_page
        assert "/api/orders/XSD1/replace-package" in order_page
        assert f"/api/tasks/{task_id}/retry" in task_page
        assert f"/api/tasks/{task_id}/claim" in task_page
        assert f"/api/tasks/{task_id}/resolve" in task_page
    finally:
        server.shutdown()


def test_admin_can_retry_task_after_worker_lease_expires(tmp_path):
    store, server, base = run_server(tmp_path)
    task_id = store.enqueue_task("review", "review:XSD1", {"order": "XSD1"})
    store.claim_task("worker", lease_seconds=-1, kind="review")
    try:
        status, result = request(base + f"/api/tasks/{task_id}/retry", "secret-token", "POST",
                                 {"operator": "ops", "reason": "lease expired"},
                                 server.csrf_token)
        assert status == 200 and result["status"] == "pending"
    finally:
        server.shutdown()


def test_admin_cannot_retry_ocr_while_worker_lease_is_active(tmp_path):
    store, server, base = run_server(tmp_path)
    store.enqueue_inbox("label-1", {"order": "XSD1", "path": "label.png"})
    store.claim_inbox("ocr-worker", lease_seconds=120)
    try:
        try:
            request(base + "/api/inbox/label-1/retry", "secret-token", "POST",
                    {"operator": "ops", "reason": "manual retry"}, server.csrf_token)
            assert False, "active OCR lease must reject retry"
        except HTTPError as error:
            assert error.code == 409
    finally:
        server.shutdown()


def test_admin_can_retry_ocr_after_worker_lease_expires(tmp_path):
    store, server, base = run_server(tmp_path)
    store.enqueue_inbox("label-1", {"order": "XSD1", "path": "label.png"})
    store.claim_inbox("ocr-worker", lease_seconds=-1)
    try:
        status, result = request(base + "/api/inbox/label-1/retry", "secret-token", "POST",
                                 {"operator": "ops", "reason": "lease expired"},
                                 server.csrf_token)
        assert status == 200 and result["status"] == "pending"
    finally:
        server.shutdown()


def test_order_detail_can_open_registered_original_file(tmp_path):
    store, server, base = run_server(tmp_path)
    original = tmp_path / "label.png"
    original.write_bytes(b"label-image")
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报"})
    store.enqueue_inbox("label-1", {"order": "XSD1", "path": str(original)})
    try:
        _, order_page = request_text(base + "/orders/XSD1", "secret-token")
        status, content_type, content = request_bytes(base + "/evidence/label-1", "secret-token")
        assert "/evidence/label-1" in order_page
        assert (status, content_type, content) == (200, "image/png", b"label-image")
    finally:
        server.shutdown()


def test_order_evidence_requires_exact_order_match(tmp_path):
    store, server, base = run_server(tmp_path)
    wrong = tmp_path / "wrong.png"
    wrong.write_bytes(b"wrong-order")
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报"})
    store.enqueue_inbox("label-10", {"order": "XSD10", "path": str(wrong)})
    try:
        _, order_page = request_text(base + "/orders/XSD1", "secret-token")
        assert "/evidence/label-10" not in order_page
    finally:
        server.shutdown()


def test_completed_ocr_keeps_exact_order_evidence_association(tmp_path):
    store, server, base = run_server(tmp_path)
    original = tmp_path / "label.png"
    original.write_bytes(b"label-image")
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报"})
    store.enqueue_inbox("label-1", {"path": str(original), "name": "label.png"})
    store.complete_inbox_with_task("label-1", "pipeline", "ocr:label-1", {}, orders=["XSD1"])
    try:
        _, order_page = request_text(base + "/orders/XSD1", "secret-token")
        assert "/evidence/label-1" in order_page
    finally:
        server.shutdown()


def test_daily_report_links_to_affected_orders(tmp_path):
    store, server, base = run_server(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报", "history": []})
    try:
        _, page = request_text(base + "/reports/daily", "secret-token")
        assert "缺面单：1" in page
        assert "href='/orders/XSD1'" in page
        assert "数据过期阈值：N/A" in page
    finally:
        server.shutdown()


def test_admin_can_create_users_without_exposing_password_hash(tmp_path):
    store, server, base = run_server(tmp_path)
    try:
        status, created = request(base + "/api/users/operator1", "secret-token", "POST",
                                  {"password": "operator-password-1", "role": "operator",
                                   "active": True, "reason": "new colleague"},
                                  server.csrf_token)
        assert status == 200 and created == {
            "username": "operator1", "role": "operator", "active": True}
        _, users = request(base + "/api/users", "secret-token")
        assert {row["username"] for row in users["items"]} == {"admin", "operator1"}
        assert all("password_hash" not in row for row in users["items"])
        _, orders = request(base + "/api/orders", "operator-password-1", username="operator1")
        assert orders == {"items": []}
        stored = store.connect().execute(
            "SELECT password_hash FROM admin_users WHERE username='operator1'").fetchone()[0]
        assert "operator-password-1" not in stored
        _, page = request_text(base + "/users", "secret-token")
        assert "权限审计" in page and "new colleague" in page
    finally:
        server.shutdown()


def test_viewer_is_read_only_and_does_not_see_action_forms(tmp_path):
    store, server, base = run_server(tmp_path)
    store.put_admin_user("viewer1", "viewer-password-1", "viewer")
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中"})
    try:
        _, page = request_text(base + "/orders/XSD1", "viewer-password-1", "viewer1")
        assert "补录人员" not in page and "class=api-form" not in page
        try:
            request(base + "/api/orders/XSD1/salesperson", "viewer-password-1", "POST",
                    {"salesperson": "张三", "user_id": "u1", "reason": "attempt"},
                    server.csrf_token, username="viewer1")
            assert False, "viewer mutation must fail"
        except HTTPError as error:
            assert error.code == 403
        try:
            request(base + "/api/users", "viewer-password-1", username="viewer1")
            assert False, "viewer user-management access must fail"
        except HTTPError as error:
            assert error.code == 403
    finally:
        server.shutdown()


def test_authenticated_operator_identity_is_used_for_audit(tmp_path):
    store, server, base = run_server(tmp_path)
    store.put_admin_user("operator1", "operator-password-1", "operator")
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报"})
    try:
        request(base + "/api/orders/XSD1/salesperson", "operator-password-1", "POST",
                {"operator": "forged-admin", "reason": "verified", "salesperson": "张三",
                 "user_id": "u1"}, server.csrf_token, username="operator1")
        assert store.list_audit("XSD1")[0]["operator"] == "operator1"
    finally:
        server.shutdown()


def test_last_active_admin_cannot_be_disabled(tmp_path):
    store, server, base = run_server(tmp_path)
    try:
        try:
            request(base + "/api/users/admin", "secret-token", "POST",
                    {"role": "viewer", "active": False, "reason": "unsafe change"},
                    server.csrf_token)
            assert False, "last active admin must remain available"
        except HTTPError as error:
            assert error.code == 400
        assert store.authenticate_admin("admin", "secret-token")["role"] == "admin"
    finally:
        server.shutdown()


def test_bootstrap_admin_rotates_with_admin_token(tmp_path):
    store = Storage(tmp_path)
    store.ensure_admin_user("admin", "first-admin-token")
    store.ensure_admin_user("admin", "second-admin-token")
    assert store.authenticate_admin("admin", "first-admin-token") is None
    assert store.authenticate_admin("admin", "second-admin-token") == {
        "username": "admin", "role": "admin"}


def test_csrf_secret_is_independent_from_admin_password(tmp_path):
    _, server, base = run_server(tmp_path)
    try:
        _, page = request_text(base + "/orders", "secret-token")
        assert server.csrf_token in page
        assert csrf_token("secret-token") not in page
    finally:
        server.shutdown()


def test_user_change_and_audit_are_atomic(tmp_path):
    store = Storage(tmp_path)
    store.ensure_admin_user("admin", "secret-token")
    with store.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_user_audit BEFORE INSERT ON audit_log
               WHEN NEW.entity_type='admin_user' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")
    try:
        store.put_admin_user("viewer1", "viewer-password-1", "viewer",
                             operator="admin", reason="new colleague")
        assert False, "audit failure must abort account change"
    except sqlite3.IntegrityError:
        pass
    assert {row["username"] for row in store.list_admin_users()} == {"admin"}


def test_malformed_active_value_is_rejected(tmp_path):
    _, server, base = run_server(tmp_path)
    try:
        try:
            request(base + "/api/users/viewer1", "secret-token", "POST",
                    {"password": "viewer-password-1", "role": "viewer",
                     "active": "ture", "reason": "typo"}, server.csrf_token)
            assert False, "malformed active flag must fail closed"
        except HTTPError as error:
            assert error.code == 400
    finally:
        server.shutdown()


def test_visual_login_session_and_logout(tmp_path):
    _, server, base = run_server(tmp_path)
    browser, cookies = session_browser()
    try:
        anonymous = browser.open(base + "/orders")
        assert anonymous.geturl().endswith("/login")
        assert "登录物流运营台" in anonymous.read().decode()

        signed_in = login(browser, base, "admin", "secret-token")
        page = signed_in.read().decode()
        assert signed_in.geturl().endswith("/orders")
        assert "当前：admin（admin）" in page
        assert any(cookie.name == "logistics_session" for cookie in cookies)

        csrf = re.search(r"name=csrf content='([^']+)'", page).group(1)
        browser.open(Request(base + "/logout",
                             data=urlencode({"csrf": csrf}).encode(), method="POST"))
        after_logout = browser.open(base + "/orders")
        assert after_logout.geturl().endswith("/login")
    finally:
        server.shutdown()


def test_api_unauthorized_response_keeps_basic_challenge(tmp_path):
    _, server, base = run_server(tmp_path)
    try:
        try:
            urlopen(base + "/api/orders")
            assert False, "authentication must be required"
        except HTTPError as error:
            assert error.code == 401
            assert error.headers["WWW-Authenticate"] == 'Basic realm="logistics-admin"'
    finally:
        server.shutdown()


def test_login_rejects_cross_site_origin_and_throttles_failures(tmp_path):
    _, server, base = run_server(tmp_path)
    browser, cookies = session_browser()
    try:
        data = urlencode({"username": "admin", "password": "secret-token"}).encode()
        try:
            browser.open(Request(base + "/login", data=data, method="POST",
                                 headers={"Origin": "https://attacker.example"}))
            assert False, "cross-site login must be rejected"
        except HTTPError as error:
            assert error.code == 403
        assert not any(cookie.name == "logistics_session" for cookie in cookies)

        login_page = browser.open(base + "/login").read().decode()
        login_csrf = re.search(r"name=csrf value='([^']+)'", login_page).group(1)
        wrong = urlencode({"username": "admin", "password": "wrong-password",
                           "csrf": login_csrf}).encode()
        try:
            browser.open(Request(base + "/login", data=wrong, method="POST"))
        except HTTPError as error:
            assert error.code == 401
        try:
            login(browser, base, "admin", "wrong-password")
            assert False, "repeated login must be throttled"
        except HTTPError as error:
            assert error.code == 429
            assert int(error.headers["Retry-After"]) >= 1
    finally:
        server.shutdown()


def test_basic_auth_uses_the_same_login_throttle(tmp_path):
    store, server, base = run_server(tmp_path)
    calls = 0
    authenticate = store.authenticate_admin

    def counted_authenticate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return authenticate(*args, **kwargs)

    store.authenticate_admin = counted_authenticate
    try:
        for _ in range(2):
            try:
                request(base + "/api/orders", "wrong-password")
            except HTTPError as error:
                assert error.code == 401
        assert calls == 1
    finally:
        server.shutdown()


def test_role_change_invalidates_existing_session(tmp_path):
    store, server, base = run_server(tmp_path)
    store.put_admin_user("viewer1", "viewer-password-1", "viewer")
    browser, _ = session_browser()
    try:
        login(browser, base, "viewer1", "viewer-password-1")
        assert "当前：viewer1（viewer）" in browser.open(base + "/orders").read().decode()

        store.put_admin_user("viewer1", "", "operator")
        response = browser.open(base + "/orders")
        assert response.geturl().endswith("/login")
        assert "当前：viewer1（operator）" in login(
            browser, base, "viewer1", "viewer-password-1").read().decode()
    finally:
        server.shutdown()
