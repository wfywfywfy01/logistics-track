#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读 ups_results.json: 台账缺的订单自动 ingest-pair, 再逐单 track-update 回填状态。"""
import subprocess, sys
import robust
from storage import Storage

store = Storage()
store.migrate_legacy_json()
res = store.get_document("ups_results", {})
sales = store.get_document("sales_map", {})
led = store.get_shipments()

def cli(args):
    r = subprocess.run([sys.executable, "tracking-pipeline.py"] + args, capture_output=True)
    return r.stdout.decode("utf-8", errors="replace").strip()

# 1) ingest missing orders (intl 一律从台账拿, 台账没有才看 sales_map 缓存)
for order, r in res.items():
    tn = r.get("tracking") or led.get(order, {}).get("intl") or (sales.get(order) or {}).get("intl")
    if order not in led and tn:
        print("ingest:", order, "->", cli(["ingest-pair", "--order", order, "--intl", tn])[:100], flush=True)

# 2) track-update with real carrier data
for order, r in res.items():
    if not r.get("ok"):
        print("skip (not ok):", order, flush=True); continue
    detail = r.get("detail") or r.get("status_en", "")
    args = ["track-update", "--order", order, "--status", r["stage"], "--detail", detail,
            "--tracking", r.get("tracking") or "", "--observed-at", r.get("observed_at") or ""]
    if r.get("binding_version") is not None:
        args += ["--binding-version", str(r["binding_version"])]
    print("update:", order, r["stage"], "->", cli(args)[:120], flush=True)
print("ALL DONE", flush=True)
