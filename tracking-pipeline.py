#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""物流小助手 本地管线（无 GUI 后台）
子命令: ingest-forecast | ingest-pair | track-update | list | notify
台账 data/shipments.json；录单人缓存 data/sales_map.json；定人缓存 data/users_map.json
"""
import argparse, json, re, subprocess, sys, os
from datetime import datetime
from pathlib import Path
import openpyxl
from storage import Storage, iso
from carriers import detect_carrier

DATA = Path((os.environ.get("LOGIBOT_DATA_DIR") or "data")); DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "shipments.json"; SALES_MAP = DATA / "sales_map.json"; USERS_MAP = DATA / "users_map.json"; ORG_PEOPLE = DATA / "org_people.json"

import robust
STORE = Storage(DATA)
STORE.migrate_legacy_json()
LEDGER_LOCK = robust.FileLock(str(DATA / ".ledger.lock"))

def load_json(p, d=None):
    if p == DB:
        return STORE.get_shipments()
    return STORE.get_document(p.stem, {} if d is None else d)

def save_json(p, obj):
    if p == DB:
        STORE.put_shipments(obj)
    else:
        STORE.put_document(p.stem, obj)

def cli(args):
    """headless vertu-cli；返回 stdout 文本或 None(Linux 参数列表防注入, Windows 回退 shell)"""
    try:
        rc, out, _ = robust.cli_run(args)
    except subprocess.TimeoutExpired:
        raise
    except Exception:
        return None
    return out if rc == 0 else None

def cli_json(args):
    out = cli(args)
    try: return json.loads(out) if out else None
    except Exception: return None

# ---------- 预报解析 ----------
def trim(v): return "" if v is None else str(v).strip()

def parse_forecast(xlsx):
    wb = openpyxl.load_workbook(xlsx, data_only=True); ws = wb.active
    data = list(ws.iter_rows(values_only=True))
    header = [trim(c) for c in data[0]]
    items, current = [], None
    for cells in data[1:]:
        m = {header[i]: trim(v) for i, v in enumerate(cells) if i < len(header)}
        order = next((v for v in m.values() if re.match(r"^(XSD|CKD)[-\w]+$", v or "")), "")
        if order:
            if current: items.append(current)
            current = {"orderNo": order,
                       "carrier": (m.get("承运商") or "").replace("国际", ""),
                       "domestic": m.get("顺丰单号") or m.get("国内单号") or "",
                       "intl": m.get("转单号") or m.get("国际单号") or "",
                       "recipient": m.get("收件方") or m.get("收件人") or "",
                       "products": [m.get("品名") or m.get("商品") or ""],
                       "note": m.get("备注") or ""}
        elif current is not None:
            p = m.get("品名") or m.get("商品") or ""
            if p and p not in current["products"]: current["products"].append(p)
    if current: items.append(current)
    return items

# ---------- 录单人 ----------
def _row_to_rec(row):
    return {"salesperson": row.get("销售人员", ""), "products": row.get("商品", ""),
            "domestic": row.get("物流单号", ""), "shipDate": row.get("最新发货日期", ""),
            "real_order": row.get("订单号", "")}

def match_sales(order_no, domestic=None):
    # 按订单号查不到时（预报里的 CKD 出库单号就是这种），用国内顺丰号反查真实销售订单号
    sm = load_json(SALES_MAP)
    if order_no in sm and sm[order_no].get("salesperson"):
        return sm[order_no]
    data = cli_json(["sales", "+orders", "--order-no", order_no, "--period", "this_year", "--limit", "10", "--no-json"])
    exact = [r for r in ((data or {}).get("rows") or []) if trim(r.get("订单号")) == order_no]
    row = exact[0] if len(exact) == 1 else {}
    if not row.get("销售人员") and domestic:
        d2 = cli_json(["sales", "+orders", "--logistics-no", domestic, "--period", "this_year", "--limit", "10", "--no-json"])
        exact = [r for r in ((d2 or {}).get("rows") or []) if trim(r.get("物流单号")) == domestic]
        row = exact[0] if len(exact) == 1 else {}
    rec = _row_to_rec(row)
    if rec["salesperson"]:
        sm[order_no] = {**sm.get(order_no, {}), **rec}
        save_json(SALES_MAP, sm)
    return sm.get(order_no, rec)

def rematch(order):
    with LEDGER_LOCK:
        return _rematch(order)

def _rematch(order):
    """未匹配订单重新走销售系统匹配(订单号+顺丰号反查), 命中即回填"""
    db = load_json(DB)
    it = db.get(order)
    if not it: return {"order": order, "matched": False}
    sales = match_sales(order, domestic=it.get("domestic"))
    if not sales.get("salesperson"):
        m = re.search(r"^(.+)-\d+$", order)
        if m:
            sales = match_sales(m.group(1), domestic=(db.get(m.group(1)) or {}).get("domestic"))
        inherit_parent(it, db)
    if sales.get("salesperson"):
        updates = {"salesperson": sales["salesperson"]}
        if sales.get("products"): updates["products"] = [sales["products"]]
        if sales.get("domestic"): updates["domestic"] = sales["domestic"]
        STORE.patch_shipment(order, updates)
        return {"order": order, "matched": True, "salesperson": sales["salesperson"]}
    return {"order": order, "matched": False}

# ---------- 命令 ----------
def ingest_forecast(xlsx):
    with LEDGER_LOCK:
        return _ingest_forecast(xlsx)

def _ingest_forecast(xlsx):
    items = parse_forecast(xlsx)
    for it in items:
        # CKD 出库单号销售系统查不到, 必须带国内顺丰号反查
        sales = match_sales(it["orderNo"], domestic=it.get("domestic")) or {}
        order = it["orderNo"]
        def merge(prev):
            prev = dict(prev or {})
            candidate = it.get("intl") or sales.get("intl") or ""
            current = prev.get("intl") or ""
            tasks = []
            if current and candidate and current != candidate:
                tasks.append({"kind": "review", "dedupe_key": f"pair-review:{order}:{candidate}",
                              "payload": {"reason": "tracking conflict", "order": order,
                                          "current": current, "candidate": candidate}})
            for field in ("domestic", "recipient", "products", "note"):
                if it.get(field) not in (None, "", []):
                    prev[field] = it[field]
            if not (current and candidate and current != candidate) and it.get("carrier"):
                prev["carrier"] = it["carrier"]
            prev["orderNo"] = order
            prev["salesperson"] = sales.get("salesperson") or prev.get("salesperson") or ""
            if sales.get("products"):
                prev["products"] = [sales["products"]]
            prev["domestic"] = sales.get("domestic") or prev.get("domestic") or ""
            if not current:
                prev["intl"] = candidate
            prev.setdefault("status", "已预报")
            prev.setdefault("history", [])
            prev.setdefault("notified_status", None)
            return prev, tasks
        STORE.mutate_shipment(order, merge, create=True)
    return {"ingested": len(items)}

def inherit_parent(it, db):
    """子单(-1/-2...)信息与父单基本相同: 继承录单人/顺丰号/产品"""
    order = it.get("orderNo", "")
    m = re.search(r"^(.+)-\d+$", order)
    if not m: return
    parent = db.get(m.group(1))
    if not parent: return
    for k in ("salesperson", "domestic", "products", "note"):
        if parent.get(k) and not it.get(k):
            it[k] = parent[k]


def ingest_pair(order, intl, force=False):
    with LEDGER_LOCK:
        return _ingest_pair(order, intl, force)

def _ingest_pair(order, intl, force=False):
    it = STORE.get_shipment(order)
    if not it:
        STORE.enqueue_task("review", f"pair-review:{order}:{intl}",
                           {"reason": "unknown order", "order": order, "intl": intl})
        return {"paired": False, "needs_review": True, "reason": "unknown order",
                "order": order, "intl": intl}
    sales = match_sales(order, domestic=it.get("domestic"))
    parent = None
    match = re.search(r"^(.+)-\d+$", order)
    if match:
        parent = STORE.get_shipment(match.group(1))
    outcome = {}

    def bind(current):
        previous = current.get("intl") or ""
        if previous and previous != intl and not force:
            outcome.update({"paired": False, "needs_review": True,
                            "reason": "tracking conflict", "order": order, "intl": intl})
            task = {"kind": "review", "dedupe_key": f"pair-review:{order}:{intl}",
                    "payload": {"reason": "tracking conflict", "order": order,
                                "current": previous, "candidate": intl}}
            return current, [task]
        if previous != intl:
            current.setdefault("binding_history", []).append(
                {"from": previous or None, "to": intl, "at": iso(), "forced": bool(force)}
            )
            current["binding_version"] = int(current.get("binding_version") or 0) + 1
        current["intl"] = intl
        detected = detect_carrier(intl)
        if detected:
            current["carrier"] = detected
        for field in ("salesperson", "domestic"):
            if sales.get(field): current[field] = sales[field]
        if sales.get("products"): current["products"] = [sales["products"]]
        if parent:
            for field in ("salesperson", "domestic", "products", "note"):
                if parent.get(field) and not current.get(field): current[field] = parent[field]
        if current.get("status", "已预报") == "已预报":
            event_at = iso()
            current.setdefault("history", []).append(
                {"from": "已预报", "to": "已出国际单", "at": event_at,
                 "observed_at": event_at, "detail": "tracking number paired"}
            )
            current["status"] = "已出国际单"
            current["status_observed_at"] = event_at
            current["needs_notify"] = True
        outcome.update({"paired": order, "intl": intl,
                        "salesperson": current.get("salesperson", ""),
                        "binding_version": current.get("binding_version", 0)})
        return current, []

    STORE.mutate_shipment(order, bind)
    return outcome

# ---------- 轨迹落台 ----------
STAGES = ["已预报", "已出国际单", "运输中", "清关中", "部分签收", "签收"]
EXCEPTIONS = ["海关扣关", "退回", "异常"]
ALIASES = {
  "label_created": "已出国际单", "label created": "已出国际单", "已出单": "已出国际单", "出单": "已出国际单",
  "in_transit": "运输中", "in transit": "运输中", "运输": "运输中", "on the way": "运输中",
  "customs": "清关中", "clearance": "清关中", "清关": "清关中",
  "delivered": "签收", "已签收": "签收",
  "seized": "海关扣关", "held": "海关扣关", "扣关": "海关扣关",
  "returned": "退回", "return": "退回",
}
def norm_status(s):
    s = trim(s)
    if s in STAGES or s in EXCEPTIONS: return s
    low = s.lower()
    for k, v in ALIASES.items():
        if k in low: return v
    return None


def _packages(shipment):
    packages = shipment.setdefault("packages", [])
    if not packages:
        for role, field in (("primary", "intl"), ("alternate", "alt_intl")):
            tracking = shipment.get(field)
            if tracking:
                declared = shipment.get("carrier") if role == "primary" else None
                packages.append({"tracking": tracking,
                    "carrier": detect_carrier(tracking, declared) or "N/A",
                    "role": role, "status": shipment.get("status", "已出国际单"),
                    "binding_version": int(shipment.get("binding_version") or 1),
                    "binding_history": [], "history": []})
    return packages


def _package_rollup(packages):
    statuses = [package.get("status", "已出国际单") for package in packages]
    if statuses and all(status == "签收" for status in statuses): return "签收"
    if "签收" in statuses: return "部分签收"
    for exception in ("海关扣关", "退回", "异常"):
        if exception in statuses: return exception
    return min(statuses, key=lambda status: STAGES.index(status) if status in STAGES else -1)


def add_package(order, tracking, carrier=""):
    detected = detect_carrier(tracking, carrier)
    if not detected:
        return {"added": False, "error": "unknown carrier"}
    outcome = {}
    def add(shipment):
        packages = _packages(shipment)
        if any(package.get("tracking") == tracking for package in packages):
            outcome.update({"added": False, "reason": "duplicate tracking"})
            return shipment, []
        version = int(shipment.get("binding_version") or 0) + 1
        package = {"tracking": tracking, "carrier": detected,
                   "role": "primary" if not packages else "package", "status": "已出国际单",
                   "binding_version": version, "binding_history": [], "history": []}
        packages.append(package)
        shipment["binding_version"] = version
        if not shipment.get("intl"):
            shipment["intl"] = tracking
            shipment["carrier"] = package["carrier"]
        old = shipment.get("status", "已预报")
        new = _package_rollup(packages)
        if old != new:
            event_at = iso(); shipment.setdefault("history", []).append(
                {"from": old, "to": new, "at": event_at, "observed_at": event_at,
                 "detail": "package added"})
            shipment["status"] = new; shipment["status_observed_at"] = event_at
            shipment["needs_notify"] = True
        outcome.update({"added": True, "tracking": tracking, "binding_version": version})
        return shipment, []
    if STORE.mutate_shipment(order, add) is None:
        return {"added": False, "error": "unknown order"}
    return outcome


def replace_package(order, current_tracking, new_tracking, operator):
    detected = detect_carrier(new_tracking)
    if not detected:
        return {"replaced": False, "error": "unknown carrier"}
    outcome = {}
    def replace(shipment):
        packages = _packages(shipment)
        package = next((item for item in packages if item.get("tracking") == current_tracking), None)
        if not package:
            outcome.update({"replaced": False, "error": "package not found"}); return shipment, []
        event = {"from": current_tracking, "to": new_tracking, "at": iso(), "operator": operator}
        package.setdefault("binding_history", []).append(event)
        package["binding_version"] = int(package.get("binding_version") or 0) + 1
        package["tracking"] = new_tracking
        package["carrier"] = detected
        package["status"] = "已出国际单"
        if shipment.get("intl") == current_tracking:
            shipment["intl"] = new_tracking; shipment["carrier"] = package["carrier"]
        shipment["binding_version"] = int(shipment.get("binding_version") or 0) + 1
        outcome.update({"replaced": True, **package})
        return shipment, []
    if STORE.mutate_shipment(order, replace) is None:
        return {"replaced": False, "error": "unknown order"}
    return outcome


def package_update(order, tracking, status, detail="", observed_at=None, binding_version=None):
    observed = parse_observed_at(observed_at)
    normalized = norm_status(status)
    outcome = {}
    def update(shipment):
        packages = _packages(shipment)
        package = next((item for item in packages if item.get("tracking") == tracking), None)
        if not package:
            stale = any(event.get("from") == tracking for item in packages
                        for event in item.get("binding_history") or [])
            outcome.update({"changed": False, "reason": "stale binding" if stale else "package not found"})
            return shipment, []
        reported_version = int(binding_version) if binding_version is not None else 0
        bound_version = int(package.get("binding_version") or 0)
        if reported_version and bound_version and reported_version != bound_version:
            outcome.update({"changed": False, "reason": "stale binding"}); return shipment, []
        if normalized is None:
            outcome.update({"changed": False, "reason": "unknown status"}); return shipment, []
        previous_observed = _datetime_value(package.get("status_observed_at"))
        if previous_observed and observed < previous_observed:
            outcome.update({"changed": False, "reason": "stale observation"}); return shipment, []
        old_package = package.get("status", "已出国际单")
        if old_package in ("签收", "退回") and normalized != old_package:
            outcome.update({"changed": False, "reason": "terminal status"}); return shipment, []
        def idx(value): return STAGES.index(value) if value in STAGES else -1
        recovered = (old_package == "清关中" and normalized == "运输中") or (
            old_package in EXCEPTIONS and normalized in ("运输中", "清关中", "签收"))
        changed = normalized != old_package and (
            normalized in EXCEPTIONS or recovered or idx(normalized) > idx(old_package))
        if changed:
            package.setdefault("history", []).append({"from": old_package, "to": normalized,
                "at": iso(), "observed_at": observed, "detail": detail[:200]})
            package["status"] = normalized; package["status_observed_at"] = observed
        package["last_observation"] = {"status": normalized, "observed_at": observed,
                                       "detail": detail[:200]}
        aggregate = _package_rollup(packages)
        old_order = shipment.get("status", "已预报")
        if aggregate != old_order:
            shipment.setdefault("history", []).append({"from": old_order, "to": aggregate,
                "at": iso(), "observed_at": observed, "tracking": tracking,
                "detail": "package status rollup"})
            shipment["status"] = aggregate; shipment["status_observed_at"] = observed
            shipment["needs_notify"] = True
        outcome.update({"changed": changed, "status": aggregate, "tracking": tracking})
        return shipment, []
    if STORE.mutate_shipment(order, update) is None:
        return {"changed": False, "error": "unknown order"}
    return outcome


def _datetime_value(value):
    return parse_observed_at(value) if value else None

def parse_observed_at(value):
    if not value:
        return iso()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return iso(parsed)


def track_update(order, status, detail="", observed_at=None, tracking=None,
                 binding_version=None):
    with LEDGER_LOCK:
        return _track_update(order, status, detail, observed_at, tracking, binding_version)

def _track_update(order, status, detail="", observed_at=None, tracking=None,
                  binding_version=None):
    ns = norm_status(status)
    seen_at = parse_observed_at(observed_at)
    result = {}

    def update(it):
        cur = it.get("status", "已预报")
        reason = None
        if ns is None:
            reason = "unknown status"
        elif binding_version is not None and int(binding_version) and int(it.get("binding_version") or 0) \
                and int(binding_version) != int(it.get("binding_version") or 0):
            reason = "stale binding"
        elif tracking and tracking not in (it.get("intl"), it.get("alt_intl")):
            reason = "stale binding"
        elif it.get("status_observed_at") and seen_at < parse_observed_at(it["status_observed_at"]):
            reason = "stale observation"
        elif cur in ("签收", "退回") and ns != cur:
            reason = "terminal status"
        if reason:
            result.update({"order": order, "status": cur, "changed": False, "reason": reason})
            return it, []
        def idx(value): return STAGES.index(value) if value in STAGES else -1
        recovered = (cur == "清关中" and ns == "运输中") or (
            cur in EXCEPTIONS and ns in ("运输中", "清关中", "签收"))
        changed = ns != cur and (ns in EXCEPTIONS or recovered or idx(ns) > idx(cur))
        if changed:
            event_at = iso()
            it.setdefault("history", []).append(
                {"from": cur, "to": ns, "at": event_at, "observed_at": seen_at,
                 "tracking": tracking, "binding_version": binding_version,
                 "detail": detail[:200]}
            )
            it["status"] = ns
            it["needs_notify"] = True
            it["status_observed_at"] = seen_at
        it["last_observation"] = {"status": ns, "observed_at": seen_at,
                                  "tracking": tracking, "detail": detail[:200]}
        result.update({"order": order, "status": it["status"], "changed": changed})
        if not changed:
            result["reason"] = "no forward transition"
        return it, []

    if STORE.mutate_shipment(order, update) is None:
        return {"error": "unknown order", "order": order}
    return result

def resolve_user(name):
    # 三级兜底: 缓存 -> 组织树全量快照 -> 实时查询(im +users --query 索引常返回空)
    um = load_json(USERS_MAP)
    org = STORE.get_document("org_people", None)
    if org is not None and name in org:
        candidates = org[name] if isinstance(org[name], list) else [org[name]]
        candidates = list(dict.fromkeys(x for x in candidates if x))
        if len(candidates) == 1:
            um[name] = candidates[0]; save_json(USERS_MAP, um)
            return um[name]
        if name in um:
            del um[name]
            save_json(USERS_MAP, um)
        return None
    if org is not None and name in um:
        del um[name]
        save_json(USERS_MAP, um)
    if name in um: return um[name]
    data = cli_json(["im", "+users", "--query", name, "--limit", "10"])
    rows = (data or {}).get("rows") or (data or {}).get("users") or []
    exact = [r for r in rows if (r.get("employee_name") or r.get("name")) == name]
    if len(exact) == 1:
        um[name] = exact[0].get("user_id") or exact[0].get("id")
        save_json(USERS_MAP, um)
        return um[name]
    return None

# ---------- 通知 ----------
def _queue_notifications(channel_id, bot_app_id=None, enqueue=True):
    queued = []
    for order, it in STORE.get_shipments().items():
        if not it.get("needs_notify"):
            continue
        status = it.get("status", "-")
        version = int(it.get("binding_version") or 0)
        h = (it.get("history") or [{}])[-1]
        event_key = h.get("at") or h.get("observed_at") or f"legacy:{status}:{version}"
        product = (it.get("products") or [""])[0]
        line = "【物流小助手】%s %s→%s｜国际单 %s｜顺丰 %s｜%s｜录单人 %s" % (
            order, h.get("from", "-"), h.get("to", status), it.get("intl") or "-",
            it.get("domestic") or "-", product[:24], it.get("salesperson") or "未匹配")
        if enqueue:
            STORE.enqueue_task("notify_group", f"group:{order}:{event_key}",
                               {"order": order, "status": status, "event_key": event_key,
                                "dedupe_key": f"group:{order}:{event_key}",
                                "channel_id": channel_id, "body": line})
        if enqueue and it.get("salesperson") and it.get("dm_notified_event") != event_key:
            dm = "你的订单 %s 物流更新：%s（国际单 %s）" % (order, status, it.get("intl") or "-")
            STORE.enqueue_task("notify_dm", f"dm:{order}:{event_key}",
                               {"order": order, "status": status, "name": it["salesperson"],
                                "uid": it.get("salesperson_id"), "event_key": event_key,
                                "bot_app_id": bot_app_id, "body": dm})
        queued.append({"order": order, "line": line})
    return queued

def _delivery_ok(output):
    if output is None or not str(output).strip():
        return False
    try:
        parsed = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return str(output).strip().lower() in {"ok", "sent", "success"}
    return isinstance(parsed, dict) and parsed.get("ok") is True


def _drain_notification_kind(kind, worker):
    results = []
    while True:
        task = STORE.claim_task(worker, lease_seconds=120, kind=kind)
        if not task:
            break
        payload = task["payload"]
        try:
            if kind == "notify_group":
                body = payload["body"]
                if body.encode("utf-8").decode("utf-8") != body or "?" in body or "�" in body:
                    raise ValueError("group message failed UTF-8 validation")
                STORE.mark_delivery_inflight(task["id"])
                out = cli(["im", "+agent-notify", "--target", "im", "--agent-slug", "logistics-track",
                           "--agent-name", "物流小助手", "--bot-name", "物流小助手",
                           "--channel-id", payload["channel_id"], "--body", payload["body"], "--no-json"])
                if not _delivery_ok(out): raise RuntimeError("group notification failed")
                STORE.complete_notification(task["id"], payload["order"], payload["event_key"],
                                            {"needs_notify": False,
                                             "notified_status": payload["status"]}, out)
            else:
                uid = payload.get("uid") or resolve_user(payload["name"])
                if not uid: raise RuntimeError("recipient is missing or ambiguous")
                app_id = payload.get("bot_app_id")
                args = (["im", "+bot-send-user", "--app-id", app_id, "--user-id", str(uid), "--body", payload["body"]]
                        if app_id else ["im", "+send-user", "--user-id", str(uid), "--body", payload["body"]])
                STORE.mark_delivery_inflight(task["id"])
                out = cli(args)
                if not _delivery_ok(out): raise RuntimeError("direct notification failed")
                STORE.complete_notification(task["id"], payload["order"], payload["event_key"],
                                            {"dm_notified_status": payload["status"],
                                             "dm_notified_event": payload["event_key"]}, out)
            results.append({"order": payload["order"], "kind": kind, "ok": True})
        except subprocess.TimeoutExpired as error:
            STORE.mark_task_unknown(task["id"], "delivery outcome unknown: " + str(error))
            results.append({"order": payload.get("order"), "kind": kind,
                            "ok": False, "unknown": True})
        except Exception as error:
            STORE.fail_task(task["id"], str(error), max_attempts=10)
            results.append({"order": payload.get("order"), "kind": kind, "ok": False})
    return results

def notify(channel_id, bot_app_id=None, dry=False):
    queued = _queue_notifications(channel_id, bot_app_id, enqueue=not dry)
    if dry:
        return {"notified": 0, "items": queued, "dry": True}
    worker = f"notify:{os.getpid()}"
    results = _drain_notification_kind("notify_group", worker)
    results.extend(_drain_notification_kind("notify_dm", worker))
    return {"notified": sum(1 for row in results if row["ok"]), "items": results,
            "failed": sum(1 for row in results if not row["ok"]), "dry": False}

def list_cmd(need_review=False):
    db = load_json(DB)
    rows = list(db.values())
    if need_review: rows = [r for r in rows if not r.get("salesperson") or not r.get("intl")]
    by_sales = {}
    for r in rows: by_sales.setdefault(r.get("salesperson") or "未匹配", []).append(r)
    out = []
    for sp, group in sorted(by_sales.items()):
        out.append({"salesperson": sp, "count": len(group),
                    "orders": [{"order": g["orderNo"], "intl": g.get("intl", ""), "domestic": g.get("domestic", ""),
                                "status": g.get("status", ""), "product": (g.get("products") or [""])[0][:30]} for g in group]})
    return {"total": len(rows), "by_salesperson": out}

def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("ingest-forecast"); a.add_argument("--file", required=True)
    b = sp.add_parser("ingest-pair"); b.add_argument("--order", required=True); b.add_argument("--intl", required=True); b.add_argument("--force", action="store_true")
    c = sp.add_parser("track-update"); c.add_argument("--order", required=True); c.add_argument("--status", required=True); c.add_argument("--detail", default=""); c.add_argument("--observed-at"); c.add_argument("--tracking"); c.add_argument("--binding-version", type=int)
    f = sp.add_parser("add-package"); f.add_argument("--order", required=True); f.add_argument("--tracking", required=True); f.add_argument("--carrier", default="")
    g = sp.add_parser("replace-package"); g.add_argument("--order", required=True); g.add_argument("--tracking", required=True); g.add_argument("--new-tracking", required=True); g.add_argument("--operator", required=True)
    h = sp.add_parser("package-update"); h.add_argument("--order", required=True); h.add_argument("--tracking", required=True); h.add_argument("--status", required=True); h.add_argument("--detail", default=""); h.add_argument("--observed-at"); h.add_argument("--binding-version", type=int)
    c = sp.add_parser("rematch"); c.add_argument("--order", required=True)
    d = sp.add_parser("list"); d.add_argument("--need-review", action="store_true")
    e = sp.add_parser("notify"); e.add_argument("--channel-id", required=True); e.add_argument("--bot-app-id"); e.add_argument("--dry", action="store_true")
    args = p.parse_args()
    rc = 0
    if args.cmd == "ingest-forecast":
        result = ingest_forecast(args.file); rc = 0 if result.get("ingested") else 2
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "ingest-pair":
        result = ingest_pair(args.order, args.intl, args.force); rc = 0 if result.get("paired") else 2
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "track-update":
        result = track_update(args.order, args.status, args.detail, args.observed_at, args.tracking, args.binding_version)
        rc = 2 if result.get("error") or result.get("reason") in ("unknown status", "stale binding") else 0
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "rematch":
        result = rematch(args.order); rc = 0 if result.get("matched") else 2
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "add-package":
        result = add_package(args.order, args.tracking, args.carrier); rc = 0 if result.get("added") else 2
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "replace-package":
        result = replace_package(args.order, args.tracking, args.new_tracking, args.operator); rc = 0 if result.get("replaced") else 2
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "package-update":
        result = package_update(args.order, args.tracking, args.status, args.detail,
                                args.observed_at, args.binding_version)
        rc = 2 if result.get("error") or result.get("reason") in ("unknown status", "stale binding") else 0
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "list": print(json.dumps(list_cmd(args.need_review), ensure_ascii=False))
    elif args.cmd == "notify":
        result = notify(args.channel_id, args.bot_app_id, args.dry); rc = 1 if result.get("failed") else 0
        print(json.dumps(result, ensure_ascii=False))
    return rc

if __name__ == "__main__":
    raise SystemExit(main())


