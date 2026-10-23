"""把本机真人浏览器抓到的 UPS 结果合并进服务端 ups_results(多包裹写 package_results)。"""
import json
import sys
from collections import defaultdict

from storage import Storage

PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/local_ups_results.json"

store = Storage()
incoming = json.load(open(PATH, encoding="utf-8-sig"))
ledger = store.get_shipments()

by_order = defaultdict(dict)
for tracking, result in incoming.items():
    order = result.get("order")
    if not order:
        continue
    by_order[order][tracking] = result

single = multi = 0


def merge(document):
    global single, multi
    document = dict(document or {})
    for order, packages in by_order.items():
        shipment = ledger.get(order) or {}
        declared = [p.get("tracking") for p in (shipment.get("packages") or []) if p.get("tracking")]
        if not declared and shipment.get("intl"):
            declared = [shipment["intl"]]
        if len(declared) > 1:
            base = next(iter(packages.values()))
            document[order] = {**base, "order": order, "package_results": packages,
                               "ok": all(item.get("ok") for item in packages.values())}
            multi += 1
        else:
            document[order] = next(iter(packages.values()))
            single += 1
    return document


store.mutate_document("ups_results", merge, {})
ok = sum(1 for item in incoming.values() if item.get("ok"))
print(json.dumps({"incoming": len(incoming), "ok": ok, "failed": len(incoming) - ok,
                  "orders_single": single, "orders_multi": multi}, ensure_ascii=False))
