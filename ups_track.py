#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""UPS 官网真实抓取通道 (patchright headed + GetStatus API 拦截)。
后台执行：窗口移出屏幕，无前台弹窗。"""
import sys, io, json, os, time
if sys.stdout is not None and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

STAGE_MAP = {
    "D": "签收", "I": "运输中", "P": "已出国际单", "M": "已出国际单", "O": "运输中",
    # X = UPS 通用异常, 具体含义按文本细分(见 track_ups), 不再直接映射海关扣关
}
MILESTONE_CN = {
    "cms.stapp.orderReceived": "已出国际单",
    "cms.stapp.weHaveYourPkg": "运输中",
}

def track_ups(tn, timeout_nav=60000, wait_ms=25000, proxy=None):
    """proxy: SOCKS5 代理 URL, 形如 socks5://user:pass@host:port。
    不传则读环境变量 UPS_PROXY。中国数据中心出口直连会被 UPS 的 Akamai 杀 HTTP2,
    必须走海外代理(实测美国节点 GetStatus 200)。"""
    if proxy is None:
        proxy = os.environ.get("UPS_PROXY") or None
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
        got = {}
        def on_resp(r):
            if "GetStatus" in r.url and "requestedTrackingNumber" not in got:
                try:
                    d = r.json()
                    tds = d.get("trackDetails") or []
                    for td in tds:
                        if td.get("trackingNumber") == tn:
                            got["data"] = td
                except Exception:
                    pass
        pg.on("response", on_resp)
        try:
            pg.goto(f"https://www.ups.com/track?tracknum={tn}&loc=en_US",
                    timeout=timeout_nav, wait_until="domcontentloaded")
            for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]:
                try:
                    el = pg.query_selector(sel)
                    if el and el.is_visible(): el.click(); pg.wait_for_timeout(1000); break
                except Exception: pass
            deadline = time.time() + wait_ms / 1000
            while time.time() < deadline and "data" not in got:
                pg.wait_for_timeout(1000)
            if "data" not in got:
                # retry once: reload the page
                try:
                    pg.reload(timeout=timeout_nav, wait_until="domcontentloaded")
                    deadline = time.time() + wait_ms / 1000
                    while time.time() < deadline and "data" not in got:
                        pg.wait_for_timeout(1000)
                except Exception:
                    pass
        finally:
            b.close()
    td = got.get("data")
    if not td:
        return {"tracking": tn, "ok": False, "error": "no GetStatus data"}
    st_type = (td.get("packageStatusType") or "").upper()
    en = (td.get("packageStatus") or "").lower()
    # X = UPS 通用 Exception, 不是海关扣关! 按文本细分
    if st_type == "X":
        if any(k in en for k in ("clearance", "customs", "seized", "held by customs")):
            stage = "海关扣关"
        elif any(k in en for k in ("on the way", "transit", "out for delivery")):
            stage = "运输中"
        else:
            stage = "异常"  # 如 Address Information Required 等需人工关注
    else:
        stage = STAGE_MAP.get(st_type)
    if not stage:
        if "delivered" in en: stage = "签收"
        elif any(k in en for k in ["on the way", "transit", "out for delivery"]): stage = "运输中"
        elif "label created" in en or "order received" in en: stage = "已出国际单"
        elif "customs" in en or "clearance" in en: stage = "清关中"
        elif "void" in en: stage = "退回"  # 作废/取消
        else: stage = "运输中"
    milestones = td.get("milestones") or []
    latest = ""
    for m in milestones:
        if m.get("isCurrent"):
            latest = f"{m.get('date','')} {m.get('time','')} {m.get('location','')} {m.get('name','')}".strip()
            break
    if not latest and milestones:
        m = milestones[-1]
        latest = f"{m.get('date','')} {m.get('time','')} {m.get('location','')} {m.get('name','')}".strip()
    return {
        "tracking": tn, "ok": True, "stage": stage,
        "status_en": td.get("packageStatus", ""),
        "progress": td.get("progressBarPercentage", ""),
        "received_by": td.get("receivedBy") or "",
        "detail": latest,
        "milestones": [{"date": m.get("date"), "time": m.get("time"), "loc": m.get("location"), "name": m.get("name")} for m in milestones],
    }

if __name__ == "__main__":
    tn = sys.argv[1]
    print(json.dumps(track_ups(tn), ensure_ascii=False))
