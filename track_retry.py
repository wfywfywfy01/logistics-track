#!/usr/bin/env python
"""Retry only failed package tracking results."""
from carriers import track
from storage import Storage, iso


def _rollup(rows):
    successful = [row for row in rows if row.get("ok") and row.get("stage")]
    statuses = [row["stage"] for row in successful]
    if statuses and len(successful) == len(rows) and all(value == "签收" for value in statuses):
        return "签收"
    if "签收" in statuses: return "部分签收"
    for value in ("海关扣关", "退回", "异常"):
        if value in statuses: return value
    rank = {"已出国际单": 0, "运输中": 1, "清关中": 2}
    return min(statuses, key=lambda value: rank.get(value, -1)) if statuses else None


def retry_failed(store, tracker=track):
    ledger = store.get_shipments()
    results = store.get_document("ups_results", {})
    attempted = failed = 0
    for order, aggregate in results.items():
        shipment = ledger.get(order) or {}
        package_results = aggregate.get("package_results")
        if package_results:
            targets = [row for row in package_results.values() if not row.get("ok")]
        elif not aggregate.get("ok"):
            targets = [aggregate]
        else:
            targets = []
        packages = {item.get("tracking"): item for item in shipment.get("packages") or []}
        for previous in targets:
            tracking = previous.get("tracking") or shipment.get("intl")
            if not tracking:
                continue
            package = packages.get(tracking) or {}
            try:
                current = tracker(tracking, package.get("carrier") or shipment.get("carrier"))
            except Exception as error:
                current = {"tracking": tracking, "ok": False, "error": str(error)[:150]}
            current.update({"order": order, "observed_at": iso(),
                "binding_version": int(package.get("binding_version") or shipment.get("binding_version") or 0),
                "salesperson": shipment.get("salesperson", ""),
                "fails": 0 if current.get("ok") else int(previous.get("fails") or 0) + 1})
            attempted += 1; failed += int(not current.get("ok"))
            def merge_document(document):
                document = dict(document or {})
                live = dict(document.get(order) or {})
                live_packages = live.get("package_results")
                if live_packages is not None:
                    live_packages = dict(live_packages)
                    live_packages[tracking] = current
                    rows = list(live_packages.values())
                    live.update({"package_results": live_packages,
                                 "ok": bool(rows) and all(row.get("ok") for row in rows),
                                 "partial": any(row.get("ok") for row in rows) and not all(row.get("ok") for row in rows),
                                 "stage": _rollup(rows)})
                else:
                    live.update(current)
                document[order] = live
                return document
            store.mutate_document("ups_results", merge_document, {})
    return {"attempted": attempted, "failed": failed}


if __name__ == "__main__":
    outcome = retry_failed(Storage())
    print("DONE", outcome, flush=True)
    raise SystemExit(1 if outcome["failed"] else 0)
