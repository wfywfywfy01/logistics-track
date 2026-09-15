"""Stable identity for one normalized official carrier result."""
import hashlib
import json


def result_hash(result):
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
