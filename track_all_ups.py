#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json, sys, threading
import robust
from storage import Storage, iso
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from carriers import track

MODE = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--mode" else "full"

store = Storage()
store.migrate_legacy_json()
db = store.get_shipments()
pairs = []
for k, v in db.items():
    packages = v.get("packages") or []
    if not packages:
        packages = ([{"tracking": v.get("intl"), "carrier": v.get("carrier"),
                      "binding_version": v.get("binding_version", 0)}] if v.get("intl") else [])
        if v.get("alt_intl") and v.get("alt_intl") != v.get("intl"):
            packages.append({"tracking": v["alt_intl"], "carrier": v.get("carrier"),
                             "binding_version": v.get("binding_version", 0)})
    for package in packages:
        if package.get("tracking"):
            pairs.append((k, package))
# 增量模式: 只抓"会变动"的单; 签收/异常只在 full(每天09:05) 里复查
if MODE == "incremental":
    pairs = [(k, p) for k, p in pairs if (db[k].get("status") or "") in ("已预报", "已出国际单", "运输中", "清关中", "部分签收", "异常")]
# 在途优先(最可能变化), 签收垫底
order_rank = {"运输中": 0, "清关中": 1, "已出国际单": 2, "已预报": 3, "异常": 4, "海关扣关": 4, "签收": 5, "退回": 5}
pairs.sort(key=lambda x: order_rank.get(db[x[0]].get("status"), 9))
expected = {}
for order, package in pairs:
    expected.setdefault(order, []).append(package["tracking"])

print("mode=%s total=%d" % (MODE, len(pairs)), flush=True)
lock = threading.Lock()
done_count = [0]

def merge_result(previous, current, is_alt=False):
    previous = previous or {}
    if is_alt:
        if current.get("ok") and not previous.get("ok"):
            return {**current, "primary_result": previous, "alt_result": current,
                    "alt_stage": current.get("stage"), "alt_detail": current.get("detail")}
        return {**previous, "alt_result": current, "alt_stage": current.get("stage"),
                "alt_detail": current.get("detail"), "alt_tracking": current.get("tracking")}
    alt = previous.get("alt_result")
    if not current.get("ok") and alt and alt.get("ok"):
        return {**alt, "primary_result": current, "alt_result": alt,
                "alt_stage": alt.get("stage"), "alt_detail": alt.get("detail")}
    result = dict(current)
    if alt:
        result.update({"alt_result": alt, "alt_stage": alt.get("stage"),
                       "alt_detail": alt.get("detail"), "alt_tracking": alt.get("tracking")})
    return result


def package_rollup(statuses, expected_count):
    if statuses and len(statuses) == expected_count and all(value == "签收" for value in statuses):
        return "签收"
    if "签收" in statuses:
        return "部分签收"
    for value in ("海关扣关", "退回", "异常"):
        if value in statuses: return value
    rank = {"已出国际单": 0, "运输中": 1, "清关中": 2}
    return min(statuses, key=lambda value: rank.get(value, -1)) if statuses else None


def merge_package_result(previous, current, expected_tracking):
    package_results = {key: value for key, value in
                       ((previous or {}).get("package_results") or {}).items()
                       if key in expected_tracking}
    package_results[current["tracking"]] = current
    known = [package_results[number] for number in expected_tracking if number in package_results]
    successful = [row for row in known if row.get("ok")]
    primary = package_results.get(expected_tracking[0]) or current
    result = dict(primary)
    result.update({"package_results": package_results,
                   "ok": len(known) == len(expected_tracking) and all(row.get("ok") for row in known),
                   "partial": bool(successful) and len(successful) != len(expected_tracking),
                   "stage": package_rollup([row["stage"] for row in successful if row.get("stage")],
                                           len(expected_tracking))})
    if not result.get("ok"):
        result["error"] = "; ".join(row.get("error", "") for row in known if not row.get("ok"))[:300]
    return result


def one(order, package):
    tn = package["tracking"]
    try:
        r = track(tn, package.get("carrier") or db[order].get("carrier"))
    except Exception as e:
        r = {"tracking": tn, "ok": False, "error": str(e)[:150]}
    r["order"] = order
    r["observed_at"] = iso()
    r["binding_version"] = int(package.get("binding_version") or 0)
    r["salesperson"] = db[order].get("salesperson", "")
    def merge_document(document):
        document = dict(document or {})
        previous = document.get(order) or {}
        package_previous = ((previous.get("package_results") or {}).get(tn) or previous)
        r["fails"] = 0 if r.get("ok") else (package_previous.get("fails") or 0) + 1
        document[order] = merge_package_result(previous, r, expected[order])
        return document
    store.mutate_document("ups_results", merge_document, {})
    with lock:
        done_count[0] += 1
        print("[%d/%d] %s %s -> %s" % (done_count[0], len(pairs), order, tn,
                                       r.get("stage") or ("ERR:" + r.get("error", "")[:40])), flush=True)


# 2 个并发浏览器(容器内存 1.5G, 再多会 OOM)
with ThreadPoolExecutor(max_workers=2) as ex:
    for order, package in pairs:
        ex.submit(one, order, package)
print("DONE", flush=True)
