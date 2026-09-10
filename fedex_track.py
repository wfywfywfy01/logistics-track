#!/usr/bin/env python
"""FedEx official-site tracker using headed Chromium and response capture."""
import json
import os
import sys
import time
from urllib.parse import quote, urlsplit


def classify_status(code, description):
    code = (code or "").upper()
    text = (description or "").lower()
    if code == "DL" or "delivered" in text:
        return "签收"
    if code == "RS" or any(x in text for x in ("return to shipper", "returned")):
        return "退回"
    if code in {"CD", "CC"} or any(x in text for x in ("clearance", "customs")):
        return "清关中"
    if code in {"DE", "SE", "CA", "DY", "EX"} or any(x in text for x in ("exception", "delay", "on hold")):
        return "异常"
    if code in {"IT", "OD", "AR", "DP", "AF"} or any(x in text for x in ("on the way", "transit", "out for delivery", "departed", "arrived")):
        return "运输中"
    if code in {"OC", "PU", "PX", "AP"} or any(x in text for x in ("label created", "picked up", "shipment information sent")):
        return "已出国际单"
    return None


def _location(value):
    if not isinstance(value, dict):
        return ""
    address = value.get("address") or value
    return " ".join(str(address.get(k) or "").strip() for k in
                    ("city", "stateOrProvinceCode", "countryCode") if address.get(k))


def parse_tracking_response(tracking, payload):
    output = (payload or {}).get("output") or payload or {}
    groups = output.get("completeTrackResults") or output.get("CompleteTrackResults") or []
    results = []
    for group in groups:
        results.extend(group.get("trackResults") or group.get("TrackResults") or [])
    match = next((row for row in results if str(
        (row.get("trackingNumberInfo") or {}).get("trackingNumber") or
        row.get("trackingNumber") or "") == str(tracking)), None)
    if not match:
        return None
    latest = match.get("latestStatusDetail") or {}
    description = latest.get("statusByLocale") or latest.get("description") or ""
    stage = classify_status(latest.get("code"), description)
    if not stage:
        return {"tracking": tracking, "ok": False, "error": "unknown FedEx status",
                "status_en": description}
    scans = match.get("scanEvents") or []
    scan = scans[0] if scans else {}
    detail = " ".join(x for x in (
        scan.get("date") or scan.get("dateAndTime") or "",
        _location(scan.get("scanLocation") or latest.get("scanLocation")),
        scan.get("eventDescription") or description,
    ) if x).strip()
    return {"tracking": tracking, "ok": True, "stage": stage,
            "status_en": description, "detail": detail}


def page_failure(tracking, statuses, body):
    if 403 in statuses:
        return {"tracking": tracking, "ok": False,
                "error": "FedEx official site access denied (HTTP 403)"}
    text = (body or "").lower().replace("’", "'")
    if "can't find that tracking number" in text or "tracking number cannot be found" in text:
        return {"tracking": tracking, "ok": False,
                "error": "FedEx tracking number not found", "not_found": True}
    return {"tracking": tracking, "ok": False,
            "error": "no official FedEx tracking data"}


def _track_once(tracking, timeout_nav, wait_ms, proxy):
    from patchright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        args = ["--no-sandbox", "--window-position=4000,4000"]
        if os.environ.get("FEDEX_DISABLE_HTTP2", os.environ.get("UPS_DISABLE_HTTP2")) == "1":
            args.append("--disable-http2")
        browser = playwright.chromium.launch(headless=False, args=args)
        options = {"locale": "en-US", "viewport": {"width": 1366, "height": 768}}
        if proxy:
            options["proxy"] = {"server": proxy}
        context = browser.new_context(**options)
        page = context.new_page()
        responses, statuses = [], []

        def receive(response):
            parsed_url = urlsplit(response.url)
            if parsed_url.netloc != "api.fedex.com" or parsed_url.path != "/track/v2/shipments":
                return
            statuses.append(response.status)
            try:
                parsed = parse_tracking_response(tracking, response.json())
                if parsed:
                    responses.append(parsed)
            except Exception:
                pass

        page.on("response", receive)
        try:
            page.goto("https://www.fedex.com/wtrk/track/?trknbr=" + quote(str(tracking)),
                      timeout=timeout_nav, wait_until="domcontentloaded")
            deadline = time.time() + wait_ms / 1000
            while time.time() < deadline and not responses:
                page.wait_for_timeout(500)
            body = page.locator("body").inner_text() if not responses else ""
        finally:
            browser.close()
    return responses[0] if responses else page_failure(tracking, statuses, body)


def track_fedex(tracking, timeout_nav=60000, wait_ms=25000, proxy=None, attempts=2):
    proxy = proxy if proxy is not None else (os.environ.get("FEDEX_PROXY") or os.environ.get("UPS_PROXY") or None)
    errors = []
    for attempt in range(attempts):
        try:
            result = _track_once(tracking, timeout_nav, wait_ms, proxy)
            if result.get("ok") or result.get("not_found"):
                return result
            errors.append(result.get("error") or "no official tracking data")
        except Exception as error:
            errors.append(type(error).__name__ + ": " + str(error)[:100])
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    return {"tracking": tracking, "ok": False, "error": "; ".join(errors)[-300:]}


if __name__ == "__main__":
    print(json.dumps(track_fedex(sys.argv[1]), ensure_ascii=False))
