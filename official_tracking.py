"""Stable identity for one normalized official carrier result."""
import hashlib
import json
import re


def delivery_confirmed(description):
    """Accept affirmative delivery statuses, never plans or substring matches."""
    return bool(re.match(
        r"^(?:(?:shipment|package|item) )?(?:(?:has been|was|is) )?"
        r"(?:successfully )?delivered(?:$|[\s.,:;-])",
        str(description or "").strip().lower()))


def result_hash(result):
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
