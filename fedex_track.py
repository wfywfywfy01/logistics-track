#!/usr/bin/env python
"""FedEx official-site tracker using headed Chromium and response capture."""
import json
import os
import sys
import time
from datetime import datetime, timezone
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


def _utc_timestamp(value):
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "N/A"
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return "N/A"


def _event(item, fallback_location=None):
    return {
        "source_time_text": item.get("date") or item.get("dateAndTime") or "",
        "occurred_at_utc": _utc_timestamp(item.get("date") or item.get("dateAndTime")),
        "location": _location(item.get("scanLocation") or fallback_location),
        "status": item.get("eventDescription") or item.get("derivedStatus") or "",
        "description": item.get("exceptionDescription") or "",
        "code": item.get("eventType") or item.get("derivedStatusCode") or "",
        "exception_code": item.get("exceptionCode") or "",
    }


def _estimated_delivery(match):
    window = match.get("estimatedDeliveryTimeWindow") or {}
    values = window.get("window") or window
    begins = values.get("begins") or values.get("begin") or ""
    ends = values.get("ends") or values.get("end") or ""
    if begins or ends:
        return {"local_from_text": begins, "local_through_text": ends,
                "from_utc": _utc_timestamp(begins), "through_utc": _utc_timestamp(ends)}
    for item in match.get("dateAndTimes") or []:
        if str(item.get("type") or "").upper() in {"ESTIMATED_DELIVERY", "ESTIMATED_DELIVERY_DATE"}:
            value = item.get("dateTime") or ""
            return {"local_from_text": value, "local_through_text": "",
                    "from_utc": _utc_timestamp(value), "through_utc": "N/A"}
    return None


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
    events = [_event(item, latest.get("scanLocation")) for item in scans]
    latest_event = events[0] if events else {
        "source_time_text": "", "occurred_at_utc": "N/A",
        "location": _location(latest.get("scanLocation")),
        "status": description, "description": "", "code": latest.get("code") or "",
        "exception_code": ""}
    return {"tracking": tracking, "ok": True, "stage": stage,
            "status_en": description, "detail": detail, "source": "fedex.com",
            "estimated_delivery": _estimated_delivery(match),
            "latest_event": latest_event, "progress_steps": [],
            "progress_steps_availability": "N/A", "events": events}


DOM_STAGES = (
    ("OUT FOR DELIVERY", "运输中"),
    ("IN TRANSIT", "运输中"),
    ("DELIVERED", "签收"),
    ("DELIVERY EXCEPTION", "异常"),
    ("SHIPMENT EXCEPTION", "异常"),
    ("CLEARANCE IN PROGRESS", "清关中"),
    ("AVAILABLE FOR CLEARANCE", "清关中"),
    ("CLEARANCE DELAY", "清关中"),
    ("LABEL CREATED", "已出国际单"),
    ("SHIPMENT INFORMATION SENT TO FEDEX", "已出国际单"),
    ("PICKED UP", "已出国际单"),
    ("RETURNED TO SHIPPER", "退回"),
    ("RETURNING TO SHIPPER", "退回"),
)


def dom_status_hint(body):
    """Map rendered FedEx page text to a stage; earliest match wins
    (the status heading appears before scan-history rows)."""
    if not body:
        return None
    upper = str(body).upper()
    best = None
    for phrase, stage in DOM_STAGES:
        pos = upper.find(phrase)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, stage, phrase)
    if not best:
        return None
    return best[1], best[2]


def parse_dom_status(tracking, body):
    """Fall back to the rendered FedEx page when the API payload is blocked."""
    if not body or not str(body).strip():
        return None
    hint = dom_status_hint(body)
    if not hint:
        return None
    stage, phrase = hint
    lines = [line.strip() for line in str(body).splitlines() if line.strip()]
    detail = ""
    for index, line in enumerate(lines):
        if phrase in line.upper():
            detail = " | ".join(lines[index + 1:index + 5])[:200]
            break
    return {"tracking": tracking, "ok": True, "stage": stage,
            "status_en": phrase, "detail": detail or phrase, "source": "dom"}


def page_failure(tracking, statuses, body):
    dom = parse_dom_status(tracking, body)
    if dom:
        return dom
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
        responses, statuses, raw_responses, parse_errors = [], [], [], []

        def receive(response):
            parsed_url = urlsplit(response.url)
            if parsed_url.netloc != "api.fedex.com" or parsed_url.path != "/track/v2/shipments":
                return
            statuses.append(response.status)
            raw_responses.append(response)

        page.on("response", receive)
        body, processed = "", 0

        def drain_responses():
            nonlocal processed
            while processed < len(raw_responses) and not responses:
                response = raw_responses[processed]
                processed += 1
                try:
                    parsed = parse_tracking_response(tracking, response.json())
                    if parsed:
                        responses.append(parsed)
                except Exception as error:
                    parse_errors.append(type(error).__name__)

        try:
            page.goto("https://www.fedex.com/wtrk/track/?trknbr=" + quote(str(tracking)),
                      timeout=timeout_nav, wait_until="domcontentloaded")
            deadline = time.time() + wait_ms / 1000
            while time.time() < deadline and not responses:
                drain_responses()
                if responses:
                    break
                page.wait_for_timeout(500)
                try:
                    body = page.locator("body").inner_text() or ""
                except Exception:
                    body = ""
                lower = body.lower()
                if dom_status_hint(body) or "can't find that tracking number" in lower or \
                        "tracking number cannot be found" in lower:
                    break
            drain_responses()
        finally:
            browser.close()
    if responses:
        return responses[0]
    fallback = page_failure(tracking, statuses, body)
    if fallback.get("ok") or fallback.get("not_found") or 403 in statuses:
        return fallback
    if parse_errors:
        return {"tracking": tracking, "ok": False,
                "error": "FedEx response parse failed: " + parse_errors[-1]}
    return fallback


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
