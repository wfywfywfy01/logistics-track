#!/usr/bin/env python
"""Authenticated operations console for logistics-track."""
import argparse
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from storage import Storage, TaskConflict
from operations import build_daily_report


ADMIN_JS = """
document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.matches('.api-form')) return;
  event.preventDefault();
  const response = await fetch(event.submitter?.formAction || form.action, {
    method: 'POST',
    headers: {'Content-Type': 'application/json',
              'X-CSRF-Token': document.querySelector('meta[name=csrf]').content},
    body: JSON.stringify(Object.fromEntries(new FormData(form)))
  });
  const result = await response.json();
  if (!response.ok) return alert(result.error || '操作失败');
  location.reload();
});
""".strip()


def csrf_token(secret):
    return hmac.new(secret.encode(), b"logistics-admin-csrf", hashlib.sha256).hexdigest()


def _orders(store, query="", status=""):
    query = query.casefold().strip()
    rows = []
    for shipment in store.get_shipments().values():
        haystack = " ".join(str(shipment.get(key) or "") for key in
                            ("orderNo", "intl", "domestic", "salesperson")).casefold()
        if query and query not in haystack:
            continue
        if status and shipment.get("status") != status:
            continue
        rows.append(shipment)
    return sorted(rows, key=lambda row: str(row.get("orderNo") or ""))


def create_server(store, token, host="127.0.0.1", port=8080):
    if not token:
        raise ValueError("ADMIN_TOKEN is required")
    store.ensure_admin_user("admin", token)
    csrf_value = secrets.token_urlsafe(32)
    session_seconds = int(float(os.environ.get("ADMIN_SESSION_HOURS") or 12) * 3600)
    if session_seconds <= 0:
        raise ValueError("ADMIN_SESSION_HOURS must be positive")
    cookie_secure = os.environ.get("ADMIN_COOKIE_SECURE") == "1"
    trust_proxy = os.environ.get("ADMIN_TRUST_PROXY") == "1"
    sessions = {}
    sessions_lock = threading.Lock()
    auth_slots = threading.BoundedSemaphore(4)
    login_failures = OrderedDict()
    login_failures_lock = threading.Lock()

    def pipeline(args):
        environment = dict(os.environ)
        environment["LOGIBOT_DATA_DIR"] = str(store.data_dir)
        result = subprocess.run([sys.executable, "tracking-pipeline.py"] + args,
                                cwd=Path(__file__).parent, env=environment,
                                capture_output=True, timeout=120)
        output = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
        try: payload = json.loads(output[-1]) if output else {"error": "empty pipeline response"}
        except json.JSONDecodeError: payload = {"error": "invalid pipeline response"}
        return result.returncode, payload

    class Handler(BaseHTTPRequestHandler):
        def _principal(self):
            if hasattr(self, "_principal_value"):
                return self._principal_value
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                cookie.clear()
            session_cookie = cookie.get("logistics_session")
            session_token = session_cookie.value if session_cookie else ""
            if session_token:
                with sessions_lock:
                    session = sessions.get(session_token)
                current = store.get_admin_principal(session["username"]) if session else None
                if (session and session["expires_at"] > time.time() and current and
                        current["auth_version"] == session["auth_version"]):
                    self._principal_value = {"username": current["username"],
                                             "role": current["role"],
                                             "_session": session_token}
                    return self._principal_value
                with sessions_lock:
                    sessions.pop(session_token, None)
            value = self.headers.get("Authorization", "")
            try:
                scheme, encoded = value.split(" ", 1)
                userpass = base64.b64decode(encoded, validate=True).decode()
                user, password = userpass.split(":", 1)
            except Exception:
                self._principal_value = None
                return None
            if scheme.lower() == "basic":
                self._principal_value, _ = self._authenticate_admin(user, password)
            else:
                self._principal_value = None
            return self._principal_value

        def _csrf(self):
            principal = self._principal()
            session_token = (principal or {}).get("_session")
            if session_token:
                with sessions_lock:
                    session = sessions.get(session_token)
                if session:
                    return session["csrf"]
            return csrf_value

        def _headers(self, status, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; "
                             "form-action 'self'; base-uri 'none'")
            if status == 401 and urlsplit(self.path).path.startswith("/api/"):
                self.send_header("WWW-Authenticate", 'Basic realm="logistics-admin"')
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _json(self, status, payload):
            self._headers(status, "application/json; charset=utf-8")
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

        def _html(self, status, body):
            self._headers(status, "text/html; charset=utf-8")
            principal = self._principal() or {"username": "-", "role": "-"}
            users_link = "<a href='/users'>权限管理</a>" if principal["role"] == "admin" else ""
            shell = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                     "<meta name=csrf content='" + self._csrf() + "'>"
                     "<title>物流运营台</title><style>body{font:15px system-ui;margin:2rem auto;max-width:1200px;"
                     "padding:0 1rem;color:#17202a;background:#f6f8fa}nav{padding:1rem;border-radius:.7rem;"
                     "background:#17202a}nav a{margin-right:1rem;color:#fff}.user{float:right;color:#c9d1d9}"
                     ".logout{float:right;margin:-.45rem 0 0 .7rem}.logout button{background:#30363d;border:0}"
                     "h1{margin-top:1.6rem}"
                     "table,pre,.api-form{background:#fff;border:1px solid #d8dee4;border-radius:.6rem}"
                     "table{border-collapse:separate;border-spacing:0;width:100%}th,td{padding:.65rem;"
                     "border-bottom:1px solid #e7ebef;text-align:left}.bad{color:#b42318;font-weight:600}"
                     "input,select,button{margin:.2rem;padding:.5rem;border:1px solid #aab2bd;border-radius:.35rem}"
                     "button{color:#fff;background:#0969da;border-color:#0969da;cursor:pointer}"
                     "form{margin:.7rem 0}.api-form{padding:.55rem}pre{white-space:pre-wrap;padding:1rem;overflow:auto}"
                     "@media(max-width:700px){body{margin:1rem auto}table{display:block;overflow:auto}"
                     "input,select,button{box-sizing:border-box;width:100%;margin:.2rem 0}}</style>"
                     "<nav><a href='/orders'>订单</a><a href='/tasks'>异常待办</a><a href='/notifications'>通知中心</a>"
                     "<a href='/reports/daily'>运营日报</a>" + users_link +
                     "<form class=logout method=post action='/logout'><input type=hidden name=csrf value='%s'>"
                     "<button>退出</button></form><span class=user>当前：%s（%s）</span></nav>" % (
                         html.escape(self._csrf()), html.escape(principal["username"]),
                         html.escape(principal["role"])) + body +
                     "<script src='/admin.js' defer></script>")
            self.wfile.write(shell.encode())

        def _login_page(self, status=200, error="", extra=None):
            login_csrf = secrets.token_urlsafe(32)
            message = "<p class=bad>%s</p>" % html.escape(error) if error else ""
            body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<title>登录物流运营台</title><style>body{font:15px system-ui;background:#f6f8fa;"
                    "display:grid;min-height:100vh;place-items:center;margin:0}.login{width:min(360px,85vw);"
                    "padding:2rem;background:#fff;border:1px solid #d8dee4;border-radius:.8rem}label{display:block;"
                    "margin:1rem 0}input,button{box-sizing:border-box;width:100%;padding:.65rem;margin-top:.35rem;"
                    "border:1px solid #aab2bd;border-radius:.4rem}button{color:#fff;background:#0969da;"
                    "border-color:#0969da}.bad{color:#b42318}</style><main class=login>"
                    "<h1>登录物流运营台</h1>" + message +
                    "<form method=post action='/login'><input type=hidden name=csrf value='%s'>"
                    "<label>用户名<input name=username required "
                    "autocomplete=username></label><label>密码<input name=password type=password required "
                    "autocomplete=current-password></label><button>登录</button></form></main>" %
                    html.escape(login_csrf))
            secure = "; Secure" if cookie_secure else ""
            headers = dict(extra or {})
            headers["Set-Cookie"] = (
                "logistics_login_csrf=%s; Path=/login; HttpOnly; SameSite=Strict; Max-Age=600%s" %
                (login_csrf, secure))
            self._headers(status, "text/html; charset=utf-8", headers)
            self.wfile.write(body.encode())

        def _same_origin_login(self, form):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except CookieError:
                cookie.clear()
            csrf_cookie = cookie.get("logistics_login_csrf")
            if not csrf_cookie or not hmac.compare_digest(
                    str(form.get("csrf") or ""), csrf_cookie.value):
                return False
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return False
            origin = self.headers.get("Origin")
            if not origin or origin == "null":
                return True
            parsed = urlsplit(origin)
            return (parsed.scheme in ("http", "https") and
                    parsed.netloc.lower() == self.headers.get("Host", "").lower())

        def _login_backoff(self, key):
            now = time.time()
            with login_failures_lock:
                entry = login_failures.get(key)
                if not entry:
                    return 0
                if entry["last_seen"] < now - 900:
                    login_failures.pop(key, None)
                    return 0
                login_failures.move_to_end(key)
                return max(0, int(entry["blocked_until"] - now + 0.999))

        def _client_ip(self):
            forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            if trust_proxy and forwarded:
                try:
                    return str(ipaddress.ip_address(forwarded))
                except ValueError:
                    pass
            return self.client_address[0]

        def _record_login_failure(self, key):
            now = time.time()
            with login_failures_lock:
                failures = login_failures.get(key, {}).get("failures", 0) + 1
                login_failures[key] = {
                    "failures": failures, "blocked_until": now + min(2 ** (failures - 1), 60),
                    "last_seen": now}
                login_failures.move_to_end(key)
                while len(login_failures) > 1024:
                    login_failures.popitem(last=False)

        def _authenticate_admin(self, username, password, include_auth_version=False):
            failure_key = (self._client_ip(), username.lower())
            retry_after = self._login_backoff(failure_key)
            if retry_after:
                return None, retry_after
            if not auth_slots.acquire(blocking=False):
                return None, 1
            try:
                principal = store.authenticate_admin(
                    username, password, include_auth_version=include_auth_version)
            finally:
                auth_slots.release()
            if principal:
                with login_failures_lock:
                    login_failures.pop(failure_key, None)
            else:
                self._record_login_failure(failure_key)
            return principal, 0

        def _redirect(self, location, cookie=None):
            extra = {"Location": location, "Content-Length": "0"}
            if cookie:
                extra["Set-Cookie"] = cookie
            self._headers(303, "text/plain; charset=utf-8", extra)

        def _read_form(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("request body too large")
            return {key: values[-1] for key, values in parse_qs(
                self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("request body too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self):
            parsed = urlsplit(self.path)
            principal = self._principal()
            if parsed.path == "/login":
                return self._redirect("/orders") if principal else self._login_page()
            if not principal:
                return (self._json(401, {"error": "authentication required"})
                        if parsed.path.startswith("/api/") else self._redirect("/login"))
            params = parse_qs(parsed.query)
            if parsed.path == "/admin.js":
                self._headers(200, "text/javascript; charset=utf-8")
                return self.wfile.write(ADMIN_JS.encode())
            if parsed.path.startswith("/evidence/"):
                item_id = unquote(parsed.path.removeprefix("/evidence/"))
                item = next((row for row in store.get_inbox() if row["id"] == item_id), None)
                path_value = ((item or {}).get("payload") or {}).get("path")
                if not path_value:
                    return self._json(404, {"error": "evidence not found"})
                path = Path(path_value).resolve()
                roots = [store.data_dir.resolve(),
                         Path(os.environ.get("LOGIBOT_TMP_DIR") or "/app/tmp").resolve()]
                if not any(path == root or path.is_relative_to(root) for root in roots):
                    return self._json(403, {"error": "evidence path is outside allowed directories"})
                if not path.is_file():
                    return self._json(404, {"error": "evidence file is missing"})
                content = path.read_bytes()
                self._headers(200, mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                              {"Content-Length": str(len(content)),
                               "Content-Disposition": 'inline; filename="%s"' % path.name.replace('"', '')})
                return self.wfile.write(content)
            if parsed.path == "/api/orders":
                return self._json(200, {"items": _orders(store,
                    (params.get("q") or [""])[0], (params.get("status") or [""])[0])})
            if parsed.path.startswith("/api/orders/"):
                order = unquote(parsed.path.removeprefix("/api/orders/"))
                shipment = store.get_shipment(order)
                if not shipment: return self._json(404, {"error": "order not found"})
                return self._json(200, {"shipment": shipment,
                    "evidence": store.evidence_for_order(order), "audit": store.list_audit(order)})
            if parsed.path == "/api/tasks":
                statuses = (params.get("status") or ["pending,retry,dead,unknown"])[0].split(",")
                return self._json(200, {"items": store.list_issues(tuple(statuses))})
            if parsed.path == "/api/notifications":
                return self._json(200, {"items": store.list_tasks(kinds=("notify_group", "notify_dm"))})
            if parsed.path == "/api/users":
                if principal["role"] != "admin":
                    return self._json(403, {"error": "permission denied"})
                return self._json(200, {"items": store.list_admin_users()})
            if parsed.path == "/api/reports/daily":
                return self._json(200, build_daily_report(
                    store, freshness_hours=os.environ.get("TRACKING_DATA_MAX_AGE_HOURS")))
            if parsed.path in ("/", "/orders"):
                rows = _orders(store, (params.get("q") or [""])[0],
                               (params.get("status") or [""])[0])
                body = "<h1>订单列表</h1><form><input name=q placeholder='订单/运单/录单人'><button>查询</button></form>"
                body += "<table><tr><th>订单</th><th>国际单</th><th>录单人</th><th>状态</th></tr>"
                for row in rows:
                    order = str(row.get("orderNo") or "")
                    body += "<tr><td><a href='/orders/%s'>%s</a></td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        quote(order), html.escape(order), html.escape(str(row.get("intl") or "-")),
                        html.escape(str(row.get("salesperson") or "未匹配")),
                        html.escape(str(row.get("status") or "N/A")))
                return self._html(200, body + "</table>")
            if parsed.path.startswith("/orders/"):
                order = unquote(parsed.path.removeprefix("/orders/")); shipment = store.get_shipment(order)
                if not shipment: return self._html(404, "<h1>订单不存在</h1>")
                endpoint = "/api/orders/" + quote(order, safe="")
                evidence = store.evidence_for_order(order)
                fields = "<input name=reason required placeholder='原因'>"
                body = "<h1>%s</h1><pre>%s</pre>" % (
                    html.escape(order), html.escape(json.dumps(shipment, ensure_ascii=False, indent=2)))
                if principal["role"] in ("admin", "operator"):
                    body += ("<h2>补录人员</h2><form class=api-form action='%s/salesperson'>"
                             "<input name=salesperson required placeholder='姓名'><input name=user_id required "
                             "placeholder='用户 ID'>%s<button>保存</button></form>") % (endpoint, fields)
                    body += ("<h2>增加包裹</h2><form class=api-form action='%s/packages'>"
                             "<input name=tracking required placeholder='国际单号'><select name=carrier>"
                             "<option>UPS</option><option>DHL</option><option>FEDEX</option></select>"
                             "%s<button>增加</button></form>") % (endpoint, fields)
                    body += ("<h2>换单</h2><form class=api-form action='%s/replace-package'>"
                             "<input name=tracking required placeholder='原国际单号'><input name=new_tracking "
                             "required placeholder='新国际单号'>%s<button>换单</button></form>") % (endpoint, fields)
                links = " ".join("<a href='/evidence/%s'>查看 %s</a>" % (
                    quote(str(item["id"]), safe=""), html.escape(str(item["id"])))
                    for item in evidence["inbox"] if (item.get("payload") or {}).get("path"))
                body += "<h2>原件与处理记录</h2>%s<pre>%s</pre><h2>审计</h2><pre>%s</pre>" % (
                    links, html.escape(json.dumps(evidence, ensure_ascii=False, indent=2)),
                    html.escape(json.dumps(store.list_audit(order), ensure_ascii=False, indent=2)))
                return self._html(200, body)
            if parsed.path == "/tasks":
                rows = store.list_issues()
                body = "<h1>异常待办</h1><table><tr><th>ID</th><th>类型</th><th>状态</th><th>原因</th><th>内容</th><th>操作</th></tr>"
                for row in rows:
                    actions = ""
                    if principal["role"] in ("admin", "operator") and row.get("source") == "task":
                        endpoint = "/api/tasks/%s" % row["id"]
                        actions = ("<form class=api-form action='%s/retry'><input name=reason required placeholder='原因'>"
                                   "<button>重试</button><button formaction='%s/claim'>认领</button>"
                                   "<button formaction='%s/resolve'>结案</button></form>") % (
                                       endpoint, endpoint, endpoint)
                    elif principal["role"] in ("admin", "operator") and row.get("source") == "inbox":
                        endpoint = "/api/inbox/%s/retry" % quote(str(row["id"]), safe="")
                        actions = ("<form class=api-form action='%s'><input name=reason required placeholder='原因'>"
                                   "<button>重试</button></form>") % endpoint
                    body += "<tr><td>%s</td><td>%s</td><td class=bad>%s</td><td>%s</td><td><pre>%s</pre></td><td>%s</td></tr>" % (
                        row["id"], html.escape(row["kind"]), html.escape(row["status"]),
                        html.escape(str(row.get("last_error") or "")),
                        html.escape(json.dumps(row["payload"], ensure_ascii=False)), actions)
                return self._html(200, body + "</table>")
            if parsed.path == "/notifications":
                rows = store.list_tasks(kinds=("notify_group", "notify_dm"))
                return self._html(200, "<h1>通知中心</h1><pre>%s</pre>" % html.escape(
                    json.dumps(rows, ensure_ascii=False, indent=2)))
            if parsed.path == "/reports/daily":
                report = build_daily_report(
                    store, freshness_hours=os.environ.get("TRACKING_DATA_MAX_AGE_HOURS"))
                def order_links(orders):
                    return " ".join("<a href='/orders/%s'>%s</a>" % (
                        quote(str(order), safe=""), html.escape(str(order))) for order in orders) or "无"
                body = "<h1>运营日报 %s</h1><p>订单总数：%s</p>" % (
                    html.escape(report["date"]), report["denominator"])
                body += "<h2>缺面单：%s</h2><p>%s</p>" % (
                    report["missing_label"]["count"], order_links(report["missing_label"]["orders"]))
                body += "<h2>今日签收：%s</h2><p>%s</p>" % (
                    report["delivered_today"]["count"], order_links(report["delivered_today"]["orders"]))
                body += "<h2>未结案：%s</h2><p><a href='/tasks'>进入异常待办</a></p>" % (
                    report["unresolved"]["count"])
                body += "<h2>承运商数据新鲜度</h2><pre>%s</pre>" % html.escape(
                    json.dumps(report["carrier_freshness"], ensure_ascii=False, indent=2))
                body += "<p>数据过期阈值：%s</p>" % html.escape(
                    str(report["tracking_data_max_age_hours"]))
                return self._html(200, body)
            if parsed.path == "/users":
                if principal["role"] != "admin":
                    return self._json(403, {"error": "permission denied"})
                body = ("<h1>权限管理</h1><p>admin：全部权限；operator：处理订单和异常；"
                        "viewer：只读查看。</p><form class=api-form action='/api/users'>"
                        "<input name=username required placeholder='用户名'><input name=password type=password "
                        "required minlength=12 placeholder='初始密码（至少 12 位）'><select name=role>"
                        "<option>viewer</option><option>operator</option><option>admin</option></select>"
                        "<input name=reason required placeholder='原因'><button>新增账号</button></form>")
                body += "<table><tr><th>用户名</th><th>角色</th><th>状态</th><th>修改</th></tr>"
                for user in store.list_admin_users():
                    endpoint = "/api/users/" + quote(user["username"], safe="")
                    active_options = ("<option value=true selected>启用</option><option value=false>停用</option>"
                                      if user["active"] else
                                      "<option value=true>启用</option><option value=false selected>停用</option>")
                    body += ("<tr><td>%s</td><td>%s</td><td>%s</td><td><form class=api-form "
                             "action='%s'><input name=password type=password minlength=12 "
                             "placeholder='留空不改密码'><select name=role><option>%s</option>"
                             "<option>admin</option><option>operator</option><option>viewer</option></select>"
                             "<select name=active>%s"
                             "</select><input name=reason required placeholder='原因'><button>保存</button>"
                             "</form></td></tr>") % (html.escape(user["username"]),
                                html.escape(user["role"]), "启用" if user["active"] else "停用",
                                endpoint, html.escape(user["role"]), active_options)
                body += "</table><h2>权限审计</h2><pre>%s</pre>" % html.escape(json.dumps(
                    [row for row in store.list_audit(limit=100) if row["entity_type"] == "admin_user"],
                    ensure_ascii=False, indent=2))
                return self._html(200, body)
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            parsed_path = urlsplit(self.path).path
            if parsed_path == "/login":
                try:
                    form = self._read_form()
                except (UnicodeDecodeError, ValueError) as error:
                    return self._login_page(400, str(error))
                if not self._same_origin_login(form):
                    return self._login_page(403, "请求来源无效")
                username = str(form.get("username") or "").strip()
                principal, retry_after = self._authenticate_admin(
                    username, str(form.get("password") or ""), include_auth_version=True)
                if retry_after:
                    return self._login_page(429, "登录尝试过多，请稍后重试",
                                            {"Retry-After": str(retry_after)})
                if not principal:
                    return self._login_page(401, "用户名或密码错误")
                session_token = secrets.token_urlsafe(32)
                with sessions_lock:
                    now = time.time()
                    for old_token, session in list(sessions.items()):
                        if session["expires_at"] <= now:
                            sessions.pop(old_token, None)
                    same_user = sorted(
                        ((old_token, session) for old_token, session in sessions.items()
                         if session["username"] == principal["username"]),
                        key=lambda item: item[1]["created_at"])
                    for old_token, _ in same_user[:-4]:
                        sessions.pop(old_token, None)
                    sessions[session_token] = {
                        "username": principal["username"], "auth_version": principal["auth_version"],
                        "created_at": now, "expires_at": now + session_seconds,
                        "csrf": secrets.token_urlsafe(32)}
                secure = "; Secure" if cookie_secure else ""
                return self._redirect(
                    "/orders", "logistics_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%s%s" % (
                        session_token, session_seconds, secure))
            if parsed_path == "/logout":
                principal = self._principal()
                if principal:
                    try:
                        form = self._read_form()
                    except (UnicodeDecodeError, ValueError) as error:
                        return self._json(400, {"error": str(error)})
                    if not hmac.compare_digest(str(form.get("csrf") or ""), self._csrf()):
                        return self._json(403, {"error": "csrf check failed"})
                    with sessions_lock:
                        sessions.pop(principal.get("_session"), None)
                return self._redirect(
                    "/login", "logistics_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            principal = self._principal()
            if not principal:
                return self._json(401, {"error": "authentication required"})
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self._csrf()):
                return self._json(403, {"error": "csrf check failed"})
            parts = parsed_path.strip("/").split("/")
            try:
                payload = self._read_json()
            except (ValueError, json.JSONDecodeError) as error:
                return self._json(400, {"error": str(error)})
            operator = principal["username"]
            reason = str(payload.get("reason") or "").strip()
            if not reason:
                return self._json(400, {"error": "reason is required"})
            if len(parts) in (2, 3) and parts[:2] == ["api", "users"]:
                if principal["role"] != "admin":
                    return self._json(403, {"error": "permission denied"})
                username = unquote(parts[2]) if len(parts) == 3 else str(payload.get("username") or "")
                password = str(payload.get("password") or "")
                role = str(payload.get("role") or "viewer").lower()
                if "active" not in payload:
                    active = None
                elif isinstance(payload["active"], bool):
                    active = payload["active"]
                elif isinstance(payload["active"], str) and payload["active"].strip().lower() in (
                        "true", "false"):
                    active = payload["active"].strip().lower() == "true"
                else:
                    return self._json(400, {"error": "active must be true or false"})
                try:
                    result = store.put_admin_user(
                        username, password, role, active, operator=operator, reason=reason)
                except ValueError as error:
                    return self._json(400, {"error": str(error)})
                return self._json(200, result)
            if principal["role"] not in ("admin", "operator"):
                return self._json(403, {"error": "permission denied"})
            if len(parts) == 4 and parts[:2] == ["api", "inbox"] and parts[3] == "retry":
                try: result = store.retry_inbox(unquote(parts[2]), operator, reason)
                except TaskConflict as error: return self._json(409, {"error": str(error)})
                return self._json(200, result) if result else self._json(404, {"error": "inbox item not found"})
            if len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "salesperson":
                name = str(payload.get("salesperson") or "").strip()
                user_id = str(payload.get("user_id") or "").strip()
                if not name or not user_id:
                    return self._json(400, {"error": "salesperson and user_id are required"})
                result = store.assign_salesperson(unquote(parts[2]), name, user_id, operator, reason)
                return self._json(200, result) if result else self._json(404, {"error": "order not found"})
            if len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "packages":
                tracking = str(payload.get("tracking") or "").strip()
                carrier = str(payload.get("carrier") or "").strip()
                if not tracking:
                    return self._json(400, {"error": "tracking is required"})
                code, result = pipeline(["add-package", "--order", unquote(parts[2]),
                                         "--tracking", tracking, "--carrier", carrier])
                if code == 0:
                    store.record_audit(unquote(parts[2]), "package", tracking, "add", operator, reason)
                    return self._json(200, result)
                return self._json(400, result)
            if len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "replace-package":
                current = str(payload.get("tracking") or "").strip()
                replacement = str(payload.get("new_tracking") or "").strip()
                code, result = pipeline(["replace-package", "--order", unquote(parts[2]),
                    "--tracking", current, "--new-tracking", replacement, "--operator", operator])
                if code == 0:
                    store.record_audit(unquote(parts[2]), "package", current, "replace", operator, reason)
                    return self._json(200, result)
                return self._json(400, result)
            if len(parts) == 4 and parts[:2] == ["api", "tasks"]:
                try: task_id = int(parts[2])
                except ValueError: return self._json(400, {"error": "invalid task id"})
                try: result = store.act_on_task(task_id, parts[3], operator, reason)
                except ValueError as error: return self._json(400, {"error": str(error)})
                except TaskConflict as error: return self._json(409, {"error": str(error)})
                return self._json(200, result) if result else self._json(404, {"error": "task not found"})
            return self._json(404, {"error": "not found"})

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.csrf_token = csrf_value
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("ADMIN_BIND") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ADMIN_PORT") or 8080))
    args = parser.parse_args()
    server = create_server(Storage(), os.environ.get("ADMIN_TOKEN") or "", args.host, args.port)
    print("admin listening on %s:%s" % server.server_address, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
