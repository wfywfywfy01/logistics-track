#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UPS 官网真实抓取通道 (patchright headed + GetStatus API 拦截)。
后台执行：窗口移出屏幕，无前台弹窗。"""
import sys, json, os, time
from datetime import datetime, timezone

STAGE_MAP = {
    "D": "签收", "I": "运输中", "P": "已出国际单", "M": "已出国际单", "O": "运输中",
    # X = UPS 通用异常, 具体含义按文本细分(见 track_ups), 不再直接映射海关扣关
}
MILESTONE_CN = {
    "cms.stapp.orderReceived": "已出国际单",
    "cms.stapp.weHaveYourPkg": "运输中",
}

def classify_status(status_type, description):
    st_type = (status_type or "").upper()
    en = (description or "").lower()
    if st_type == "X":
        if any(k in en for k in ("clearance", "customs", "seized", "held by customs")):
            return "海关扣关"
        if any(k in en for k in ("on the way", "transit", "out for delivery")):
            return "运输中"
        return "异常"
    stage = STAGE_MAP.get(st_type)
    if stage:
        return stage
    if "delivered" in en: return "签收"
    if any(k in en for k in ("on the way", "transit", "out for delivery")): return "运输中"
    if "label created" in en or "order received" in en: return "已出国际单"
    if "customs" in en or "clearance" in en: return "清关中"
    if "void" in en: return "退回"
    return None


def _compact_date(value):
    value = str(value or "")
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def _compact_time(value):
    value = str(value or "")
    if len(value) == 6 and value.isdigit():
        return f"{value[:2]}:{value[2:4]}:{value[4:]}"
    return value


def _ups_utc(date_value, time_value):
    raw_date = str(date_value or "")
    raw_time = str(time_value or "").replace(":", "")
    try:
        parsed = datetime.strptime(raw_date + raw_time, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return "N/A"


def _local_to_utc(date_value, time_value, offset):
    date_text, time_text = _compact_date(date_value), _compact_time(time_value)
    if not date_text or not time_text or not offset:
        return "N/A"
    try:
        parsed = datetime.fromisoformat(f"{date_text}T{time_text}{offset}")
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return "N/A"


def _ups_event(item):
    milestone = item.get("milestoneName") or {}
    return {
        "source_time_text": " ".join(x for x in (
            item.get("date") or "", item.get("time") or "") if x),
        "occurred_at_utc": _ups_utc(item.get("gmtDate"), item.get("gmtTime")),
        "timezone_offset": item.get("gmtOffset") or "",
        "location": item.get("location") or "",
        "status": milestone.get("name") or item.get("activityScan") or "",
        "description": item.get("activityScan") or "",
        "additional_description": item.get("activityAdditionalDescription") or "",
        "code": item.get("actCode") or "",
        "exception_code": item.get("exceptionCodes") or "",
        "is_brokerage": bool(item.get("isBrokerageEvent")),
    }


def parse_tracking_response(tn, payload):
    if not isinstance(payload, dict):
        return None
    wanted = str(tn).strip().upper()
    detail = next((item for item in (payload.get("trackDetails") or [])
                   if str(item.get("trackingNumber") or "").strip().upper() == wanted), None)
    if not detail:
        return None
    error_text = detail.get("errorText") or ""
    if str(detail.get("errorCode") or "") == "504" or "not found" in error_text.lower():
        return {"tracking": tn, "ok": False, "not_found": True,
                "error": "UPS tracking number not found"}
    status_type = (detail.get("packageStatusType") or "").upper()
    status_text = detail.get("packageStatus") or ""
    stage = classify_status(status_type, status_text)
    if not stage:
        return {"tracking": tn, "ok": False, "error": "unknown UPS status",
                "status_en": status_text}
    milestones = detail.get("milestones") or []
    latest = detail.get("currentMilestone") or next(
        (item for item in milestones if item.get("isCurrent")), None)
    if not latest and milestones:
        latest = milestones[-1]
    latest_text = ""
    if latest:
        latest_text = f"{latest.get('date','')} {latest.get('time','')} " \
                      f"{latest.get('location','')} {latest.get('name','')}".strip()
    events = [_ups_event(item) for item in (detail.get("shipmentProgressActivities") or [])]
    latest_event = events[0] if events else ({
        "source_time_text": " ".join(x for x in (
            latest.get("date") or "", latest.get("time") or "") if x),
        "occurred_at_utc": "N/A", "timezone_offset": "",
        "location": latest.get("location") or "", "status": latest.get("name") or "",
        "description": "", "additional_description": "", "code": "",
        "exception_code": "", "is_brokerage": False,
    } if latest else None)
    eta = None
    if detail.get("sdd"):
        offset = (detail.get("shipmentGMTInfo") or {}).get("shipToGMTOffset") or ""
        eta = {"local_date_text": _compact_date(detail.get("sdd")),
               "local_time_text": _compact_time(detail.get("sdt")),
               "timezone_offset": offset,
               "from_utc": _local_to_utc(detail.get("sdd"), detail.get("sdt"), offset)}
    progress_steps = [{
        "name": item.get("name") or "", "source_time_text": " ".join(x for x in (
            item.get("date") or "", item.get("time") or "") if x),
        "location": item.get("location") or "",
        "completed": bool(item.get("isCompleted")), "current": bool(item.get("isCurrent")),
        "future": bool(item.get("isFuture")),
    } for item in milestones]
    return {
        "tracking": tn, "ok": True, "stage": stage,
        "status_en": status_text,
        "source": "ups.com",
        "progress": detail.get("progressBarPercentage", ""),
        "progress_type": detail.get("progressBarType") or "",
        "progress_steps_availability": "available" if progress_steps else "N/A",
        "received_by": detail.get("receivedBy") or "",
        "detail": latest_text,
        "estimated_delivery": eta,
        "latest_event": latest_event,
        "progress_steps": progress_steps,
        "events": events,
        # Compatibility for callers that already use the old key.
        "milestones": [{"date": item.get("date"), "time": item.get("time"),
                        "loc": item.get("location"), "name": item.get("name")}
                       for item in milestones],
    }

def _track_once(tn, timeout_nav, wait_ms, proxy):
    """proxy: SOCKS5 代理 URL, 形如 socks5://user:pass@host:port。
    不传则读环境变量 UPS_PROXY。中国数据中心出口直连会被 UPS 的 Akamai 杀 HTTP2,
    必须走海外代理(实测美国节点 GetStatus 200)。"""
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        args = ["--no-sandbox", "--window-position=4000,4000"]
        if os.environ.get("UPS_DISABLE_HTTP2") == "1":
            args.append("--disable-http2")
        b = p.chromium.launch(headless=False, args=args)
        kw = {"locale": "en-US", "viewport": {"width":1366, "height":768}}
        if proxy:
            kw["proxy"] = {"server": proxy}
        ctx = b.new_context(**kw)
        pg = ctx.new_page()
        responses, parse_errors = [], []
        def on_resp(r):
            if "GetStatus" in r.url:
                responses.append(r)
        pg.on("response", on_resp)
        result, processed = None, 0

        def drain_responses():
            nonlocal result, processed
            while processed < len(responses) and not result:
                response = responses[processed]
                processed += 1
                try:
                    result = parse_tracking_response(tn, response.json())
                except Exception as error:
                    parse_errors.append(type(error).__name__)

        try:
            pg.goto(f"https://www.ups.com/track?tracknum={tn}&loc=en_US",
                    timeout=timeout_nav, wait_until="domcontentloaded")
            for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]:
                try:
                    el = pg.query_selector(sel)
                    if el and el.is_visible(): el.click(); pg.wait_for_timeout(1000); break
                except Exception: pass
            deadline = time.time() + wait_ms / 1000
            while time.time() < deadline and not result:
                drain_responses()
                if result:
                    break
                pg.wait_for_timeout(1000)
            drain_responses()
        finally:
            b.close()
    if result:
        return result
    if parse_errors:
        return {"tracking": tn, "ok": False,
                "error": "UPS response parse failed: " + parse_errors[-1]}
    return {"tracking": tn, "ok": False, "error": "no GetStatus data"}


def track_ups(tn, timeout_nav=60000, wait_ms=25000, proxy=None, attempts=2):
    """Retry with a fresh browser because stale UPS sessions commonly miss GetStatus."""
    proxy = proxy if proxy is not None else (os.environ.get("UPS_PROXY") or None)
    last = None
    for attempt in range(attempts):
        try:
            last = _track_once(tn, timeout_nav, wait_ms, proxy)
        except Exception as error:
            last = {"tracking": tn, "ok": False,
                    "error": type(error).__name__ + ": " + str(error)[:150]}
        if last.get("ok") or last.get("not_found") or last.get("error") == "unknown UPS status":
            return last
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    return last

if __name__ == "__main__":
    tn = sys.argv[1]
    print(json.dumps(track_ups(tn), ensure_ascii=False))
