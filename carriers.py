"""Carrier detection and dispatch for official tracking channels."""
import re


def detect_carrier(tracking, declared=""):
    declared = (declared or "").upper().replace("国际", "").strip()
    aliases = {"UPS": "UPS", "DHL": "DHL", "FEDEX": "FEDEX", "联邦快递": "FEDEX"}
    if declared in aliases:
        return aliases[declared]
    number = re.sub(r"[\s-]", "", str(tracking or "")).upper()
    if re.fullmatch(r"1Z[0-9A-Z]{16}", number):
        return "UPS"
    if re.fullmatch(r"\d{10}", number):
        return "DHL"
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", number):
        return "FEDEX"
    return None


def track(tracking, declared=""):
    carrier = detect_carrier(tracking, declared)
    if carrier == "UPS":
        from ups_track import track_ups
        result = track_ups(tracking)
    elif carrier == "DHL":
        from dhl_track import track_dhl
        result = track_dhl(tracking)
    elif carrier == "FEDEX":
        from fedex_track import track_fedex
        result = track_fedex(tracking)
    else:
        return {"tracking": tracking, "ok": False, "error": "unknown carrier"}
    result["carrier"] = carrier
    return result
