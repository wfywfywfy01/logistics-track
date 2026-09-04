#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DHL 官网抓取通道 (patchright headed + utapi 拦截)。
与 UPS 同一技术栈: 有头 Chromium + --disable-http2(容器) + 海外代理。
用法: from dhl_track import track_dhl; track_dhl("9941305430")
"""
import json, os, sys, time

def track_dhl(tn, timeout_nav=60000, wait_ms=25000, proxy=None):
    if proxy is None:
        proxy = os.environ.get("UPS_PROXY") or None
    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        args = ["--no-sandbox", "--window-position=4000,4000"]
        if os.environ.get("UPS_DISABLE_HTTP2") == "1":
            args.append("--disable-http2")
        b = p.chromium.launch(headless=False, args=args)
        kw = {"locale": "en-US", "viewport": {"width": 1366, "height": 768}}
        if proxy:
            kw["proxy"] = {"server": proxy}
        ctx = b.new_context(**kw)
        pg = ctx.new_page()
        got = {}

        def on_resp(r):
            if "utapi" in r.url and ("trackingNumber=" + tn) in r.url:
                try:
                    d = r.json()
                    for s in (d.get("shipments") or []):
                        if s.get("id") == tn:
                            got["data"] = s
                except Exception:
                    pass

        pg.on("response", on_resp)
        try:
            pg.goto("https://www.dhl.com/us-en/home/tracking.html",
                    timeout=timeout_nav, wait_until="domcontentloaded")
            pg.wait_for_timeout(3500)
            for sel in ["#onetrust-accept-btn-handler",
                        "button:has-text('Accept All')",
                        "button:has-text('Strictly Necessary Only')"]:
                el = pg.query_selector(sel)
                if el and el.is_visible():
                    try:
                        el.click()
                        pg.wait_for_timeout(1200)
                        break
                    except Exception:
                        pass
            el = pg.query_selector("input[name=tracking-id]")
            if el and el.is_visible():
                el.fill(tn)
                el.press("Enter")
            deadline = time.time() + wait_ms / 1000
            while time.time() < deadline and "data" not in got:
                pg.wait_for_timeout(1000)
            if "data" not in got:
                # 重试一次: 重新填单号提交
                try:
                    el = pg.query_selector("input[name=tracking-id]")
                    if el and el.is_visible():
                        el.fill(tn)
                        el.press("Enter")
                    deadline = time.time() + wait_ms / 1000
                    while time.time() < deadline and "data" not in got:
                        pg.wait_for_timeout(1000)
                except Exception:
                    pass
        finally:
            b.close()
    s = got.get("data")
    if not s:
        return {"tracking": tn, "ok": False, "error": "no utapi data"}
    st = s.get("status") or {}
    code = (st.get("statusCode") or "").lower()
    desc = st.get("description") or ""
    loc = ((st.get("location") or {}).get("address") or {}).get("addressLocality") or ""
    low = desc.lower()
    if "delivered" in code or "delivered" in low:
        stage = "签收"
    elif any(k in low for k in ("returned", "return to", "returning")) or "return" in code:
        stage = "退回"
    elif any(k in low for k in ("exception", "on hold", "held")) or "exception" in code:
        stage = "异常"
    elif "clearance" in low or "custom" in low:
        stage = "清关中"
    elif "transit" in code or any(k in low for k in ("processed", "departed", "arrived")):
        stage = "运输中"
    elif "picked" in code or "pickup" in low or "received" in low:
        stage = "已出国际单"
    else:
        stage = "运输中"
    detail = "%s %s %s" % ((st.get("timestamp") or "")[:10], loc, desc)
    return {"tracking": tn, "ok": True, "stage": stage,
            "status_en": desc, "detail": detail.strip()}


if __name__ == "__main__":
    print(json.dumps(track_dhl(sys.argv[1]), ensure_ascii=False))
