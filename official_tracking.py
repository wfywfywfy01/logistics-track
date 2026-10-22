"""Stable identity for one normalized official carrier result."""
import hashlib
import json
import re
from datetime import datetime


def delivery_confirmed(description):
    """Accept affirmative delivery statuses, never plans or substring matches."""
    return bool(re.match(
        r"^(?:(?:shipment|package|item) )?(?:(?:has been|was|is) )?"
        r"(?:successfully )?delivered(?:$|[\s.,:;-])",
        str(description or "").strip().lower()))


def result_matches_package(result, package):
    """Do not reuse a previous binding's result after a tracking number returns."""
    if not isinstance(result, dict) or not isinstance(package, dict):
        return False
    tracking = str(package.get("tracking") or "").strip().upper()
    if not tracking or str(result.get("tracking") or "").strip().upper() != tracking:
        return False
    try:
        reported = int(str(result.get("binding_version") or 0))
        bound = int(str(package.get("binding_version") or 0))
    except (TypeError, ValueError):
        return False
    if reported < 0 or bound < 0:
        return False
    if (package.get("binding_history") or bound > 1) and (not reported or not bound):
        return False
    return not (reported and bound and reported != bound)


def latest_event(events, fallback=None):
    """Prefer the newest known instant; keep source order when times are unknown."""
    candidates = list(events or []) + ([fallback] if fallback else [])
    dated = []
    for index, event in enumerate(candidates):
        try:
            occurred = datetime.fromisoformat(str(event.get("occurred_at_utc") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            if index == 0:
                return event
            continue
        if occurred.tzinfo is not None:
            dated.append((occurred, event))
        elif index == 0:
            return event
    return max(dated, key=lambda item: item[0])[1] if dated else next(iter(candidates), None)


def result_hash(result):
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
