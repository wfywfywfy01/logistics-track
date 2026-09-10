#!/usr/bin/env python
"""FedEx official-site tracker using the page's tracking JSON response."""
import json
import os
import sys
import threading
import time

_TOKEN = {"value": None, "expires_at": 0}
_TOKEN_LOCK = threading.Lock()


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


def _official_track(tracking):
    from curl_cffi import requests
    base = os.environ.get("FEDEX_API_BASE") or "https://apis.fedex.com"
    client_id = os.environ["FEDEX_CLIENT_ID"]
    client_secret = os.environ["FEDEX_CLIENT_SECRET"]
    proxy = os.environ.get("FEDEX_API_PROXY") or None
    now = time.time()
    with _TOKEN_LOCK:
        if not _TOKEN["value"] or now >= _TOKEN["expires_at"]:
            response = requests.post(
                base + "/oauth/token",
                data={"grant_type": "client_credentials", "client_id": client_id,
                      "client_secret": client_secret},
                proxy=proxy, timeout=30,
            )
            response.raise_for_status()
            token = response.json()
            _TOKEN["value"] = token["access_token"]
            _TOKEN["expires_at"] = now + int(token.get("expires_in") or 3600) - 60
    response = requests.post(
        base + "/track/v1/trackingnumbers",
        headers={"Authorization": "Bearer " + _TOKEN["value"],
                 "Content-Type": "application/json", "X-locale": "en_US"},
        json={"includeDetailedScans": True,
              "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking}}]},
        proxy=proxy, timeout=45,
    )
    response.raise_for_status()
    result = parse_tracking_response(tracking, response.json())
    return result or {"tracking": tracking, "ok": False,
                      "error": "no official FedEx API data"}


def _track_once(tracking, timeout_nav, wait_ms, proxy):
    from patchright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        args = ["--no-sandbox", "--window-position=4000,4000"]
        if os.environ.get("FEDEX_DISABLE_HTTP2") == "1":
            args.append("--disable-http2")
        browser = playwright.chromium.launch(headless=False, args=args)
        options = {"locale": "en-US", "viewport": {"width": 1366, "height": 768}}
        if proxy:
            options["proxy"] = {"server": proxy}
        context = browser.new_context(**options)
        page = context.new_page()
        responses = []

        def receive(response):
            if "track" not in response.url.lower():
                return
            try:
                parsed = parse_tracking_response(tracking, response.json())
                if parsed:
                    responses.append(parsed)
            except Exception:
                pass

        page.on("response", receive)
        try:
            page.goto("https://www.fedex.com/wtrk/track/",
                      timeout=timeout_nav, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            cookie = page.get_by_text("REJECT OPTIONAL COOKIES", exact=True)
            if cookie.count():
                cookie.first.click()
                page.wait_for_timeout(500)
            page.get_by_text("Track Another Shipment", exact=True).first.click()
            page.wait_for_timeout(500)
            inputs = page.locator("input:visible")
            for index in range(inputs.count()):
                field = inputs.nth(index)
                if field.get_attribute("type") == "text" and field.get_attribute("id") != "search":
                    field.fill(tracking)
                    break
            else:
                raise RuntimeError("FedEx tracking input unavailable")
            page.get_by_text("Track", exact=True).last.click()
            deadline = time.time() + wait_ms / 1000
            while time.time() < deadline and not responses:
                page.wait_for_timeout(500)
        finally:
            browser.close()
    return responses[0] if responses else None


def track_fedex(tracking, timeout_nav=60000, wait_ms=25000, proxy=None, attempts=2):
    api_error = ""
    if os.environ.get("FEDEX_CLIENT_ID") and os.environ.get("FEDEX_CLIENT_SECRET"):
        try:
            return _official_track(tracking)
        except Exception as error:
            api_error = "official API " + type(error).__name__ + ": " + str(error)[:120]
    proxy = proxy if proxy is not None else (os.environ.get("FEDEX_PROXY") or os.environ.get("UPS_PROXY") or None)
    errors = []
    for attempt in range(attempts):
        try:
            result = _track_once(tracking, timeout_nav, wait_ms, proxy)
            if result:
                return result
            errors.append("no official tracking data")
        except Exception as error:
            errors.append(type(error).__name__ + ": " + str(error)[:100])
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    if api_error:
        errors.insert(0, api_error)
    return {"tracking": tracking, "ok": False, "error": "; ".join(errors)[-300:]}


if __name__ == "__main__":
    print(json.dumps(track_fedex(sys.argv[1]), ensure_ascii=False))
