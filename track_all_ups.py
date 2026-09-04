#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json, sys, threading
import robust
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from ups_track import track_ups
from dhl_track import track_dhl

MODE = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--mode" else "full"

db = json.load(open("data/shipments.json", encoding="utf-8"))
pairs = []
for k, v in db.items():
    if v.get("intl"):
        pairs.append((k, v["intl"], ""))
    if v.get("alt_intl") and v.get("alt_intl") != v.get("intl"):
        pairs.append((k, v["alt_intl"], "alt_"))
# 增量模式: 只抓"会变动"的单; 签收/异常只在 full(每天09:05) 里复查
if MODE == "incremental":
    pairs = [(k, t, tag) for k, t, tag in pairs if (db[k].get("status") or "") in ("已预报", "已出国际单", "运输中", "清关中", "异常")]
# 在途优先(最可能变化), 签收垫底
order_rank = {"运输中": 0, "清关中": 1, "已出国际单": 2, "已预报": 3, "异常": 4, "海关扣关": 4, "签收": 5, "退回": 5}
pairs.sort(key=lambda x: order_rank.get(db[x[0]].get("status"), 9))

print("mode=%s total=%d" % (MODE, len(pairs)), flush=True)
results = {}
# 增量模式保留上一次的完整结果(被跳过的签收单不丢, 表格/台账不受影响)
if MODE == "incremental":
    try:
        results = json.load(open("data/ups_results.json", encoding="utf-8"))
    except Exception:
        results = {}
lock = threading.Lock()
done_count = [0]


def one(order, tn, tag=""):
    try:
        if tn.startswith("1Z"):
            r = track_ups(tn)
            r["carrier"] = "UPS"
        else:
            r = track_dhl(tn)
            r["carrier"] = "DHL"
    except Exception as e:
        r = {"tracking": tn, "ok": False, "error": str(e)[:150]}
    r["order"] = order
    r["salesperson"] = db[order].get("salesperson", "")
    if tag:
        r["alt_tracking"] = tn
    else:
        r["alt_tracking"] = ""
    try:
        prev = results.get(order) or {}
        r["fails"] = (prev.get("fails") or 0) + (0 if r.get("ok") else 1)
    except Exception:
        pass
    with lock:
        prev = results.get(order) or {}
        if tag:
            if not prev.get("ok"):
                results[order] = r
            else:
                prev["alt_stage"] = r.get("stage")
                prev["alt_detail"] = r.get("detail")
                prev["alt_tracking"] = tn
                results[order] = prev
        else:
            results[order] = r
        done_count[0] += 1
        robust.atomic_write_json("data/ups_results.json", results)
        print("[%d/%d] %s %s -> %s" % (done_count[0], len(pairs), order, tn,
                                       r.get("stage") or ("ERR:" + r.get("error", "")[:40])), flush=True)


# 2 个并发浏览器(容器内存 1.5G, 再多会 OOM)
with ThreadPoolExecutor(max_workers=2) as ex:
    for order, tn, tag in pairs:
        ex.submit(one, order, tn, tag)
print("DONE", flush=True)
