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
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from carriers import detect_carrier
from storage import Storage, TaskConflict
from operations import build_daily_report
import admin_ui


def parse_content_length(value):
    try:
        length = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid Content-Length")
    if length < 0 or length > 65536:
        raise ValueError("request body too large" if length > 65536 else "invalid Content-Length")
    return length


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, max_workers=64, **kwargs):
        self._request_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


ADMIN_JS = """
document.addEventListener('submit', async event => {
  const form = event.target;
  if (!form.matches('.api-form')) return;
  event.preventDefault();
  if (!form.reportValidity()) return;
  const button = event.submitter;
  if (button?.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
  const feedback = document.querySelector('.action-feedback');
  const buttons = [...form.querySelectorAll('button')];
  buttons.forEach(item => item.disabled = true);
  if (feedback) { feedback.hidden = false; feedback.textContent = '正在提交…'; }
  try {
    const response = await fetch(button?.formAction || form.action, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json',
                'X-CSRF-Token': document.querySelector('meta[name=csrf]').content},
      body: JSON.stringify(Object.fromEntries(new FormData(form)))
    });
    const result = await response.json();
    if (response.status === 401) {
      location.href = document.querySelector('form.logout').action.replace(/logout$/, 'login');
      return;
    }
    if (!response.ok) throw new Error(result.error || `操作失败（HTTP ${response.status}）`);
    location.reload();
  } catch (error) {
    if (feedback) feedback.textContent = error.message || '网络错误，请重试';
    buttons.forEach(item => item.disabled = false);
  }
});
document.getElementById('nav-toggle')?.addEventListener('click', event => {
  const nav = document.getElementById('admin-nav');
  const open = nav.classList.toggle('sidenav--open');
  event.currentTarget.setAttribute('aria-expanded', String(open));
});
""".strip()


def csrf_token(secret):
    return hmac.new(secret.encode(), b"logistics-admin-csrf", hashlib.sha256).hexdigest()


def _orders(store, query="", status=""):
    query = query.casefold().strip()
    rows = []
    for shipment in store.get_shipments().values():
        haystack = " ".join(str(shipment.get(key) or "") for key in
                            ("orderNo", "intl", "alt_intl", "domestic", "salesperson"))
        haystack += " " + " ".join(str(package.get("tracking") or "")
                                     for package in shipment.get("packages") or [])
        haystack = haystack.casefold()
        if query and query not in haystack:
            continue
        if status and shipment.get("status") != status:
            continue
        rows.append(shipment)
    return sorted(rows, key=lambda row: str(row.get("orderNo") or ""))


def _official_tracking_html(shipment):
    sections = []
    for package in shipment.get("packages") or []:
        official = package.get("official_tracking") or {}
        if not official:
            continue
        latest = official.get("latest_event") or {}
        eta = official.get("estimated_delivery")
        eta_text = json.dumps(eta, ensure_ascii=False) if isinstance(eta, (dict, list)) else str(eta or "N/A")
        rows = []
        for event in official.get("events") or []:
            occurred = str(event.get("occurred_at_utc") or event.get("source_time_text") or "N/A")
            rows.append("<tr><td><time datetime='%s'>%s</time></td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                html.escape(occurred, quote=True), html.escape(occurred),
                html.escape(str(event.get("status") or "N/A")),
                html.escape(str(event.get("location") or "N/A")),
                html.escape(str(event.get("description") or ""))))
        sections.append(
            "<section class=tracking><h3>%s · %s</h3><p>状态：%s｜预计送达：%s｜最后位置：%s｜来源：%s</p>"
            "<table><tr><th>时间</th><th>状态</th><th>地点</th><th>详情</th></tr>%s</table></section>" % (
                html.escape(str(package.get("carrier") or "N/A")),
                html.escape(str(package.get("tracking") or "N/A")),
                html.escape(str(latest.get("status") or official.get("status_en") or package.get("status") or "N/A")),
                html.escape(eta_text), html.escape(str(latest.get("location") or "N/A")),
                html.escape(str(official.get("source") or "N/A")), "".join(rows)))
    return "<h2>官网轨迹</h2>" + "".join(sections) if sections else ""


def official_view(package):
    """官网快照视图(official_tracking)。"""
    official = package.get("official_tracking") or {}
    events = official.get("events") or []
    return {
        "source": official.get("source"),
        "observed_at": official.get("observed_at"),
        "status_en": official.get("status_en"),
        "progress": official.get("progress"),
        "estimated_delivery": official.get("estimated_delivery"),
        "latest_event": official.get("latest_event"),
        "events": events,
        "event_count": len(events),
        "progress_steps": official.get("progress_steps") or [],
    }


def event_text(event):
    if not isinstance(event, dict):
        return ""
    parts = [event.get("occurred_at_utc"), event.get("source_time_text"),
             event.get("location"), event.get("status"), event.get("description")]
    seen, ordered = set(), []
    for part in parts:
        text = " ".join(str(part or "").split())
        if text and text != "N/A" and text not in seen:
            seen.add(text)
            ordered.append(text)
    return " ".join(ordered)[:200]


def _tracking_attempt(result, tracking):
    result = result or {}
    current = (result.get("package_results") or {}).get(tracking)
    if current is None and result.get("tracking") == tracking:
        current = result
    if not current:
        return None
    return {"observed_at": current.get("observed_at") or "N/A",
            "ok": bool(current.get("ok")), "error": current.get("error") or ""}


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def package_views(shipment, tracking_result=None, max_age_hours=None, now=None):
    """包裹视图;没有 packages 结构的历史订单按 intl/alt_intl 合成。"""
    packages = shipment.get("packages")
    if not packages:
        packages = []
        for role, field in (("primary", "intl"), ("alternate", "alt_intl")):
            tracking = shipment.get(field)
            if not tracking:
                continue
            packages.append({"tracking": tracking, "role": role,
                             "carrier": detect_carrier(tracking, shipment.get("carrier")) or "N/A",
                             "status": shipment.get("status", "已预报")})
    views = []
    for package in packages:
        view = {key: package.get(key) for key in (
            "tracking", "carrier", "role", "status", "binding_version",
            "status_observed_at", "last_observation")}
        view["official"] = official_view(package)
        view["last_tracking_attempt"] = _tracking_attempt(
            tracking_result, package.get("tracking"))
        observed = _timestamp(view["official"].get("observed_at"))
        if max_age_hours is None or observed is None:
            view["tracking_data_stale"] = "N/A"
        else:
            reference = (now or datetime.now(UTC)).astimezone(UTC)
            view["tracking_data_stale"] = (
                reference - observed.astimezone(UTC)).total_seconds() >= max_age_hours * 3600
        views.append(view)
    return views


def track_view(shipment, tracking_result=None, max_age_hours=None, now=None):
    packages = package_views(shipment, tracking_result, max_age_hours, now)
    latest_item = None
    latest_timestamp = None
    for item in packages:
        event = item["official"]["latest_event"]
        if not event:
            continue
        parsed_timestamp = _timestamp(event.get("occurred_at_utc"))
        timestamp = parsed_timestamp.timestamp() if parsed_timestamp else None
        if latest_item is None or (timestamp is not None and
                                   (latest_timestamp is None or timestamp > latest_timestamp)):
            latest_item, latest_timestamp = item, timestamp
    latest = latest_item["official"]["latest_event"] if latest_item else None
    attempts = [item["last_tracking_attempt"] for item in packages
                if item["last_tracking_attempt"]]
    latest_attempt = max(attempts, key=lambda item: (
        _timestamp(item["observed_at"]) or datetime.min.replace(tzinfo=UTC))) if attempts else None
    stale_values = [item["tracking_data_stale"] for item in packages
                    if item["tracking_data_stale"] != "N/A"]
    return {
        "order": shipment.get("orderNo"),
        "status": shipment.get("status", "已预报"),
        "salesperson": shipment.get("salesperson") or "未匹配",
        "salesperson_id": shipment.get("salesperson_id"),
        "carrier": shipment.get("carrier") or "",
        "intl": shipment.get("intl") or "",
        "alt_intl": shipment.get("alt_intl") or "",
        "domestic": shipment.get("domestic") or "",
        "products": shipment.get("products") or [],
        "status_observed_at": shipment.get("status_observed_at"),
        "latest_event": latest,
        "latest_event_tracking": latest_item.get("tracking") if latest_item else None,
        "latest_event_text": event_text(latest),
        "last_tracking_attempt": latest_attempt,
        "tracking_data_max_age_hours": max_age_hours if max_age_hours is not None else "N/A",
        "tracking_data_stale": any(stale_values) if stale_values else "N/A",
        "packages": packages,
        "history": shipment.get("history") or [],
    }


def find_by_tracking(store, number, results=None, max_age_hours=None):
    matches = []
    for order, shipment in store.get_shipments().items():
        for package in package_views(
                shipment, (results or {}).get(order), max_age_hours):
            if package.get("tracking") == number:
                matches.append({"order": order, "status": shipment.get("status", "已预报"),
                                "salesperson": shipment.get("salesperson") or "未匹配",
                                "package": package})
    return matches


def stats_view(store):
    counts = {}
    missing_intl = 0
    with_events = 0
    for shipment in store.get_shipments().values():
        status = shipment.get("status") or "已预报"
        counts[status] = counts.get(status, 0) + 1
        if not shipment.get("intl"):
            missing_intl += 1
        if any((package.get("official_tracking") or {}).get("events")
               for package in shipment.get("packages") or []):
            with_events += 1
    return {"total": sum(counts.values()), "by_status": counts,
            "missing_intl": missing_intl, "with_official_events": with_events}


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
    request_timeout = float(os.environ.get("ADMIN_REQUEST_TIMEOUT_SECONDS") or 15)
    if request_timeout <= 0:
        raise ValueError("ADMIN_REQUEST_TIMEOUT_SECONDS must be positive")
    max_age_value = os.environ.get("TRACKING_DATA_MAX_AGE_HOURS")
    tracking_max_age_hours = float(max_age_value) if max_age_value else None
    if tracking_max_age_hours is not None and tracking_max_age_hours <= 0:
        raise ValueError("TRACKING_DATA_MAX_AGE_HOURS must be positive")
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
        def setup(self):
            super().setup()
            self.connection.settimeout(request_timeout)

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
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                             "form-action 'self'; base-uri 'none'")
            if status == 401 and urlsplit(self.path).path.startswith("/api/"):
                self.send_header("WWW-Authenticate", 'Basic realm="logistics-admin"')
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _json(self, status, payload):
            self._headers(status, "application/json; charset=utf-8")
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

        def _html(self, status, body, section="orders"):
            self._headers(status, "text/html; charset=utf-8")
            principal = self._principal() or {"username": "-", "role": "-"}
            self.wfile.write(admin_ui.shell(body, principal, self._csrf(), section).encode())

        def _login_page(self, status=200, error="", extra=None):
            login_csrf = secrets.token_urlsafe(32)
            message = "<p class=bad>%s</p>" % html.escape(error) if error else ""
            body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                    "<title>登录物流运营台</title><style>body{font:14px system-ui;background:#0b0d13;"
                    "color:#e2e8f0;display:grid;min-height:100vh;place-items:center;margin:0}"
                    ".login{box-sizing:border-box;width:min(390px,92vw);padding:2rem;background:#13182a;"
                    "border:1px solid rgba(255,255,255,.14);border-radius:16px}label{display:block;"
                    "margin:1rem 0}input,button{box-sizing:border-box;width:100%;padding:.7rem;margin-top:.35rem;"
                    "border:1px solid rgba(255,255,255,.2);border-radius:10px}input{background:#0e1220;"
                    "color:#e2e8f0}button{color:#0b0d13;background:#6bb0ff;border-color:#6bb0ff;"
                    "font-weight:600;cursor:pointer}:focus-visible{outline:2px solid #6bb0ff;"
                    "outline-offset:2px}.bad{color:#fb7185}</style><main class=login>"
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
            length = parse_content_length(self.headers.get("Content-Length"))
            return {key: values[-1] for key, values in parse_qs(
                self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}

        def _read_json(self):
            length = parse_content_length(self.headers.get("Content-Length"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                try:
                    if not store.path.is_file():
                        raise FileNotFoundError(store.path)
                    with store.connect() as connection:
                        database = connection.execute("PRAGMA quick_check").fetchone()[0]
                        tables = {row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"shipments", "tasks", "admin_users"}.issubset(tables):
                        database = "error"
                except Exception:
                    database = "error"
                return self._json(200 if database == "ok" else 503,
                                  {"ok": database == "ok", "database": database})
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
            if parsed.path == "/admin.css":
                self._headers(200, "text/css; charset=utf-8")
                return self.wfile.write((Path(__file__).parent / "admin_ui.css").read_bytes())
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
            tracking_results = store.get_document("ups_results", {}) if (
                parsed.path == "/api/track" or parsed.path.startswith("/api/track/") or
                parsed.path == "/api/shipments") else {}
            if parsed.path == "/api/track":
                order = (params.get("order") or [""])[0].strip()
                number = (params.get("tracking") or [""])[0].strip()
                if order:
                    shipment = store.get_shipment(order)
                    if not shipment:
                        return self._json(404, {"ok": False, "error": "order not found"})
                    return self._json(200, {"ok": True, "shipment": track_view(
                        shipment, tracking_results.get(order), tracking_max_age_hours)})
                if not number:
                    return self._json(400, {"ok": False, "error": "order or tracking is required"})
                matches = find_by_tracking(
                    store, number, tracking_results, tracking_max_age_hours)
                if not matches:
                    return self._json(404, {"ok": False, "tracking": number, "count": 0,
                                           "matches": [], "error": "tracking number not found"})
                return self._json(200, {"ok": True, "tracking": number,
                                        "count": len(matches), "matches": matches})
            if parsed.path.startswith("/api/track/"):
                order = unquote(parsed.path.removeprefix("/api/track/"))
                shipment = store.get_shipment(order)
                if shipment:
                    return self._json(200, {"ok": True, "shipment": track_view(
                        shipment, tracking_results.get(order), tracking_max_age_hours)})
                matches = find_by_tracking(
                    store, order, tracking_results, tracking_max_age_hours)
                if matches:
                    return self._json(200, {"ok": True, "tracking": order,
                                            "count": len(matches), "matches": matches})
                return self._json(404, {"ok": False, "error": "order or tracking number not found"})
            if parsed.path == "/api/stats":
                return self._json(200, {"ok": True, **stats_view(store)})
            if parsed.path == "/api/shipments":
                rows = _orders(store, (params.get("q") or [""])[0], (params.get("status") or [""])[0])
                total = len(rows)
                try:
                    limit = max(1, min(1000, int((params.get("limit") or ["200"])[0])))
                    offset = max(0, int((params.get("offset") or ["0"])[0]))
                except ValueError:
                    return self._json(400, {"ok": False, "error": "limit/offset must be integers"})
                items = []
                for shipment in rows[offset:offset + limit]:
                    view = track_view(
                        shipment, tracking_results.get(shipment.get("orderNo")),
                        tracking_max_age_hours)
                    items.append({
                        "order": view["order"], "status": view["status"],
                        "carrier": view["carrier"], "intl": view["intl"],
                        "alt_intl": view["alt_intl"], "domestic": view["domestic"],
                        "salesperson": view["salesperson"],
                        "product": (view["products"] or [""])[0],
                        "latest_event": view["latest_event"],
                        "latest_event_text": view["latest_event_text"],
                        "last_tracking_attempt": view["last_tracking_attempt"],
                        "tracking_data_stale": view["tracking_data_stale"],
                        "event_count": sum(item["official"]["event_count"]
                                           for item in view["packages"]),
                        "status_observed_at": view["status_observed_at"],
                    })
                return self._json(200, {"ok": True, "total": total, "count": len(items),
                                        "offset": offset, "limit": limit, "items": items})
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
                results = store.get_document("ups_results", {})
                views = [track_view(row, results.get(row.get("orderNo")), tracking_max_age_hours)
                         for row in rows]
                try: page_number = int((params.get("page") or ["1"])[0])
                except ValueError: page_number = 1
                return self._html(200, admin_ui.orders(views, stats_view(store),
                    (params.get("q") or [""])[0], (params.get("status") or [""])[0],
                    (params.get("carrier") or [""])[0].upper(), page_number), "orders")
            if parsed.path.startswith("/orders/"):
                order = unquote(parsed.path.removeprefix("/orders/")); shipment = store.get_shipment(order)
                if not shipment: return self._html(404, "<h1>订单不存在</h1>")
                evidence = store.evidence_for_order(order)
                view = track_view(shipment,
                    store.get_document("ups_results", {}).get(order), tracking_max_age_hours)
                body = admin_ui.order_detail(shipment, view, evidence, store.list_audit(order),
                                             store.list_issues(limit=100000), principal["role"])
                return self._html(200, body, "orders")
            if parsed.path == "/tasks":
                try: page_number = int((params.get("page") or ["1"])[0])
                except ValueError: page_number = 1
                filters = {key: (params.get(key) or [""])[0] for key in ("kind", "status", "order")}
                return self._html(200, admin_ui.tasks(store.list_issues(limit=100000),
                    principal["role"], filters, page_number), "tasks")
            if parsed.path == "/notifications":
                rows = store.list_tasks(limit=100000, kinds=("notify_group", "notify_dm"))
                try: page_number = int((params.get("page") or ["1"])[0])
                except ValueError: page_number = 1
                return self._html(200, admin_ui.notifications(rows,
                    {"status": (params.get("status") or [""])[0]}, page_number), "notifications")
            if parsed.path == "/reports/daily":
                report = build_daily_report(
                    store, freshness_hours=os.environ.get("TRACKING_DATA_MAX_AGE_HOURS"))
                return self._html(200, admin_ui.report(report), "reports")
            if parsed.path == "/users":
                if principal["role"] != "admin":
                    return self._json(403, {"error": "permission denied"})
                audit = [row for row in store.list_audit(limit=100)
                         if row["entity_type"] == "admin_user"]
                return self._html(200, admin_ui.users(store.list_admin_users(), audit), "users")
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
                                         "--tracking", tracking, "--carrier", carrier,
                                         "--operator", operator, "--reason", reason])
                if code == 0:
                    return self._json(200, result)
                return self._json(400, result)
            if len(parts) == 4 and parts[:2] == ["api", "orders"] and parts[3] == "replace-package":
                current = str(payload.get("tracking") or "").strip()
                replacement = str(payload.get("new_tracking") or "").strip()
                code, result = pipeline(["replace-package", "--order", unquote(parts[2]),
                    "--tracking", current, "--new-tracking", replacement, "--operator", operator,
                    "--reason", reason])
                if code == 0:
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

    server = BoundedThreadingHTTPServer((host, port), Handler)
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
