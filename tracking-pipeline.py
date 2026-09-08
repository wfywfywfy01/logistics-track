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
        it["salesperson"] = sales["salesperson"]
        if sales.get("products"): it["products"] = [sales["products"]]
        if sales.get("domestic"): it["domestic"] = sales["domestic"]
        save_json(DB, db)
        return {"order": order, "matched": True, "salesperson": sales["salesperson"]}
    return {"order": order, "matched": False}

# ---------- 命令 ----------
def ingest_forecast(xlsx):
    with LEDGER_LOCK:
        return _ingest_forecast(xlsx)

def _ingest_forecast(xlsx):
    db = load_json(DB)
    items = parse_forecast(xlsx)
    for it in items:
        # CKD 出库单号销售系统查不到, 必须带国内顺丰号反查
        sales = match_sales(it["orderNo"], domestic=it.get("domestic")) or {}
        order = it["orderNo"]
        prev = db.get(order)
        it["salesperson"] = sales.get("salesperson") or it.get("salesperson") or (prev or {}).get("salesperson") or ""
        if sales.get("products"): it["products"] = [sales["products"]]
        it["domestic"] = sales.get("domestic") or it.get("domestic") or (prev or {}).get("domestic") or ""
        it["intl"] = it.get("intl") or (prev or {}).get("intl") or sales.get("intl") or ""
        if prev:
            # 已存在的订单: 合并, 保留状态/历史/通知标记(重复预报不得洗掉已追踪状态)
            keep = {k: prev.get(k) for k in ("status", "history", "notified_status",
                                            "needs_notify", "customs_alerted",
                                            "dm_notified_status", "dm_log", "note")}
            for k, v in keep.items():
                if v not in (None, "", []):
                    it[k] = v
        it.setdefault("status", "已预报"); it.setdefault("history", [])
        it.setdefault("notified_status", None)
        db[order] = it
    save_json(DB, db)
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
    db = load_json(DB)
    it = db.get(order)
    if not it:
        STORE.enqueue_task("review", f"pair-review:{order}:{intl}",
                           {"reason": "unknown order", "order": order, "intl": intl})
        return {"paired": False, "needs_review": True, "reason": "unknown order",
                "order": order, "intl": intl}
    previous = it.get("intl") or ""
    if previous and previous != intl and not force:
        STORE.enqueue_task("review", f"pair-review:{order}:{intl}",
                           {"reason": "tracking conflict", "order": order,
                            "current": previous, "candidate": intl})
        return {"paired": False, "needs_review": True, "reason": "tracking conflict",
                "order": order, "intl": intl}
    if previous != intl:
        it.setdefault("binding_history", []).append(
            {"from": previous or None, "to": intl, "at": iso(), "forced": bool(force)}
        )
        it["binding_version"] = int(it.get("binding_version") or 0) + 1
    it["intl"] = intl
    sales = match_sales(order, domestic=it.get("domestic"))
    for k in ("salesperson", "domestic"):
        if sales.get(k): it[k] = sales[k]
    if sales.get("products"): it["products"] = [sales["products"]]
    inherit_parent(it, db)
    if it.get("status", "已预报") == "已预报":
        observed_at = iso()
        it.setdefault("history", []).append(
            {"from": "已预报", "to": "已出国际单", "at": observed_at,
             "observed_at": observed_at, "detail": "tracking number paired"}
        )
        it["status"] = "已出国际单"
        it["status_observed_at"] = observed_at
        it["needs_notify"] = True
    db[order] = it; save_json(DB, db)
    return {"paired": order, "intl": intl, "salesperson": it.get("salesperson", ""),
            "binding_version": it.get("binding_version", 0)}

# ---------- 轨迹落台 ----------
STAGES = ["已预报", "已出国际单", "运输中", "清关中", "签收"]
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
    db = load_json(DB)
    it = db.get(order)
    if not it: return {"error": "unknown order", "order": order}
    ns = norm_status(status)
    cur = it.get("status", "已预报")
    if ns is None:
        return {"order": order, "status": cur, "changed": False,
                "reason": "unknown status"}
    if binding_version is not None and int(binding_version) != int(it.get("binding_version") or 0):
        return {"order": order, "status": cur, "changed": False,
                "reason": "stale binding"}
    if tracking and tracking not in (it.get("intl"), it.get("alt_intl")):
        return {"order": order, "status": cur, "changed": False,
                "reason": "stale binding"}
    observed_at = parse_observed_at(observed_at)
    previous_observed = it.get("status_observed_at")
    if previous_observed and observed_at < parse_observed_at(previous_observed):
        return {"order": order, "status": cur, "changed": False,
                "reason": "stale observation"}
    if cur in ("签收", "退回") and ns != cur:
        return {"order": order, "status": cur, "changed": False,
                "reason": "terminal status"}
    def idx(x): return STAGES.index(x) if x in STAGES else -1
    changed = (ns in EXCEPTIONS and ns != cur) or (idx(ns) > idx(cur))
    if changed:
        it.setdefault("history", []).append(
            {"from": cur, "to": ns, "at": iso(), "observed_at": observed_at,
             "tracking": tracking, "binding_version": binding_version,
             "detail": detail[:200]}
        )
        it["status"] = ns; it["needs_notify"] = True
        it["status_observed_at"] = observed_at
    it["last_observation"] = {"status": ns, "observed_at": observed_at,
                              "tracking": tracking, "detail": detail[:200]}
    db[order] = it; save_json(DB, db)
    return {"order": order, "status": it["status"], "changed": changed,
            **({} if changed else {"reason": "no forward transition"})}

def resolve_user(name):
    # 三级兜底: 缓存 -> 组织树全量快照 -> 实时查询(im +users --query 索引常返回空)
    um = load_json(USERS_MAP)
    if name in um: return um[name]
    org = load_json(ORG_PEOPLE)
    if name in org:
        candidates = org[name] if isinstance(org[name], list) else [org[name]]
        candidates = list(dict.fromkeys(x for x in candidates if x))
        if len(candidates) == 1:
            um[name] = candidates[0]; save_json(USERS_MAP, um)
            return um[name]
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
                               {"order": order, "status": status, "channel_id": channel_id, "body": line})
        if enqueue and it.get("salesperson") and it.get("dm_notified_event") != event_key:
            dm = "你的订单 %s 物流更新：%s（国际单 %s）" % (order, status, it.get("intl") or "-")
            STORE.enqueue_task("notify_dm", f"dm:{order}:{event_key}",
                               {"order": order, "status": status, "name": it["salesperson"],
                                "event_key": event_key, "bot_app_id": bot_app_id, "body": dm})
        queued.append({"order": order, "line": line})
    return queued

def _drain_notification_kind(kind, worker):
    results = []
    while True:
        task = STORE.claim_task(worker, lease_seconds=120, kind=kind)
        if not task:
            break
        payload = task["payload"]
        try:
            if kind == "notify_group":
                out = cli(["im", "+agent-notify", "--target", "im", "--agent-slug", "logistics-track",
                           "--agent-name", "物流小助手", "--bot-name", "物流小助手",
                           "--channel-id", payload["channel_id"], "--body", payload["body"], "--no-json"])
                if out is None: raise RuntimeError("group notification failed")
                STORE.patch_shipment(payload["order"], {"needs_notify": False,
                                     "notified_status": payload["status"]})
            else:
                uid = resolve_user(payload["name"])
                if not uid: raise RuntimeError("recipient is missing or ambiguous")
                app_id = payload.get("bot_app_id")
                args = (["im", "+bot-send-user", "--app-id", app_id, "--user-id", str(uid), "--body", payload["body"]]
                        if app_id else ["im", "+send-user", "--user-id", str(uid), "--body", payload["body"]])
                if cli(args) is None: raise RuntimeError("direct notification failed")
                STORE.patch_shipment(payload["order"], {"dm_notified_status": payload["status"],
                                     "dm_notified_event": payload["event_key"]})
            STORE.complete_task(task["id"])
            results.append({"order": payload["order"], "kind": kind, "ok": True})
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
    elif args.cmd == "list": print(json.dumps(list_cmd(args.need_review), ensure_ascii=False))
    elif args.cmd == "notify":
        result = notify(args.channel_id, args.bot_app_id, args.dry); rc = 1 if result.get("failed") else 0
        print(json.dumps(result, ensure_ascii=False))
    return rc

if __name__ == "__main__":
    raise SystemExit(main())


