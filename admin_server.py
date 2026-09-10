#!/usr/bin/env python
"""Authenticated operations console for logistics-track."""
import argparse
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import subprocess
import sys
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
        def _authorized(self):
            value = self.headers.get("Authorization", "")
            try:
                scheme, encoded = value.split(" ", 1)
                userpass = base64.b64decode(encoded, validate=True).decode()
                user, password = userpass.split(":", 1)
            except Exception:
                return False
            return scheme.lower() == "basic" and user == "admin" and hmac.compare_digest(password, token)

        def _headers(self, status, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="logistics-admin"')
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()

        def _json(self, status, payload):
            self._headers(status, "application/json; charset=utf-8")
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

        def _html(self, status, body):
            self._headers(status, "text/html; charset=utf-8")
            shell = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                     "<meta name=csrf content='" + csrf_token(token) + "'>"
                     "<title>物流运营台</title><style>body{font:15px system-ui;margin:2rem auto;max-width:1200px;"
                     "padding:0 1rem;color:#17202a;background:#f6f8fa}nav{padding:1rem;border-radius:.7rem;"
                     "background:#17202a}nav a{margin-right:1rem;color:#fff}h1{margin-top:1.6rem}"
                     "table,pre,.api-form{background:#fff;border:1px solid #d8dee4;border-radius:.6rem}"
                     "table{border-collapse:separate;border-spacing:0;width:100%}th,td{padding:.65rem;"
                     "border-bottom:1px solid #e7ebef;text-align:left}.bad{color:#b42318;font-weight:600}"
                     "input,select,button{margin:.2rem;padding:.5rem;border:1px solid #aab2bd;border-radius:.35rem}"
                     "button{color:#fff;background:#0969da;border-color:#0969da;cursor:pointer}"
                     "form{margin:.7rem 0}.api-form{padding:.55rem}pre{white-space:pre-wrap;padding:1rem;overflow:auto}"
                     "@media(max-width:700px){body{margin:1rem auto}table{display:block;overflow:auto}"
                     "input,select,button{box-sizing:border-box;width:100%;margin:.2rem 0}}</style>"
                     "<nav><a href='/orders'>订单</a><a href='/tasks'>异常待办</a><a href='/notifications'>通知中心</a>"
                     "<a href='/reports/daily'>运营日报</a></nav>" + body +
                     "<script src='/admin.js' defer></script>")
            self.wfile.write(shell.encode())

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise ValueError("request body too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self):
            if not self._authorized():
                return self._json(401, {"error": "authentication required"})
            parsed = urlsplit(self.path)
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
                fields = ("<input name=operator required placeholder='操作人'>"
                          "<input name=reason required placeholder='原因'>")
                body = "<h1>%s</h1><pre>%s</pre>" % (
                    html.escape(order), html.escape(json.dumps(shipment, ensure_ascii=False, indent=2)))
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
                    if row.get("source") == "task":
                        endpoint = "/api/tasks/%s" % row["id"]
                        actions = ("<form class=api-form action='%s/retry'><input name=operator required "
                                   "placeholder='操作人'><input name=reason required placeholder='原因'>"
                                   "<button>重试</button><button formaction='%s/claim'>认领</button>"
                                   "<button formaction='%s/resolve'>结案</button></form>") % (
                                       endpoint, endpoint, endpoint)
                    elif row.get("source") == "inbox":
                        endpoint = "/api/inbox/%s/retry" % quote(str(row["id"]), safe="")
                        actions = ("<form class=api-form action='%s'><input name=operator required "
                                   "placeholder='操作人'><input name=reason required placeholder='原因'>"
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
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._json(401, {"error": "authentication required"})
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token(token)):
                return self._json(403, {"error": "csrf check failed"})
            parts = urlsplit(self.path).path.strip("/").split("/")
            try:
                payload = self._read_json()
            except (ValueError, json.JSONDecodeError) as error:
                return self._json(400, {"error": str(error)})
            operator = str(payload.get("operator") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            if not operator or not reason:
                return self._json(400, {"error": "operator and reason are required"})
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

    return ThreadingHTTPServer((host, port), Handler)


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
