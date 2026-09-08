#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只重抓 ups_results.json 里 ok:false 的单(漏抓/超时补抓)。单号取台账, 1Z 走 UPS 其余走 DHL。"""
import json, sys
sys.path.insert(0, ".")
import robust
from storage import Storage
from ups_track import track_ups
from dhl_track import track_dhl

store = Storage()
store.migrate_legacy_json()
led = store.get_shipments()
prev = store.get_document("ups_results", {})
retry = [k for k, v in prev.items() if not v.get("ok")]
print("retry", retry, flush=True)
for order in retry:
    tn = prev[order].get("tracking") or (led.get(order) or {}).get("intl")
    if not tn:
        print(order, "no tracking number, skip", flush=True); continue
    try:
        r = track_ups(tn) if tn.startswith("1Z") else track_dhl(tn)
        r["carrier"] = "UPS" if tn.startswith("1Z") else "DHL"
    except Exception as e:
        r = {"tracking": tn, "ok": False, "error": str(e)[:150]}
    r["order"] = order
    r["salesperson"] = (led.get(order) or {}).get("salesperson", "")
    r["fails"] = 0 if r.get("ok") else (prev[order].get("fails") or 0) + 1
    prev[order] = r
    print(f"{order} {tn} -> {r.get('stage') or ('ERR:' + r.get('error', '')[:60])}", flush=True)
    store.put_document("ups_results", prev)
print("DONE", flush=True)
