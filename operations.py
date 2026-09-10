"""Operational exception generation and daily reporting."""
import argparse
import json
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from carriers import detect_carrier
from storage import Storage


def _datetime(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def refresh_operational_tasks(store, now=None, thresholds=None, freshness_hours=None):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    thresholds = {str(k).upper(): float(v) for k, v in (thresholds or {}).items() if v not in (None, "")}
    freshness_hours = float(freshness_hours) if freshness_hours not in (None, "") else None
    reported = {carrier: thresholds.get(carrier, "N/A") for carrier in ("UPS", "DHL", "FEDEX")}
    shipments = store.get_shipments()
    results = store.get_document("ups_results", {})
    created = []
    for order, shipment in shipments.items():
        if not shipment.get("intl") and not shipment.get("packages"):
            continue
        result = results.get(order) or {}
        packages = [item for item in shipment.get("packages") or [] if item.get("active", True)]
        package_results = result.get("package_results") or {}
        if package_results and packages:
            evaluations = []
            for package in packages:
                tracking = package.get("tracking")
                current = dict(package_results.get(tracking) or {})
                current.setdefault("tracking", tracking)
                current.setdefault("carrier", package.get("carrier"))
                if tracking not in package_results:
                    current.update({"ok": False, "error": "package result is missing"})
                evaluations.append(current)
        elif package_results:
            evaluations = list(package_results.values())
        else:
            evaluations = [result]

        failures = stale_results = 0
        for current in evaluations:
            tracking = current.get("tracking") or shipment.get("intl")
            carrier = current.get("carrier") or detect_carrier(tracking, shipment.get("carrier"))
            observed_value = current.get("observed_at") or "N/A"
            if not current.get("ok") or observed_value == "N/A":
                reason = ("official tracking failed" if not current.get("ok") else
                          "official tracking observation time unavailable")
                error = current.get("error") or ("observed_at is missing" if observed_value == "N/A" else "N/A")
                store.enqueue_task(
                    "tracking_failure", f"tracking-failure:{order}:{tracking}:{observed_value}", {
                        "order": order, "tracking": tracking or "N/A", "carrier": carrier or "N/A",
                        "reason": reason, "observed_at": observed_value, "error": error})
                created.append((order, "tracking_failure")); failures += 1
                continue
            result_observed = _datetime(observed_value)
            package_terminal = current.get("stage") in ("签收", "退回")
            if (not package_terminal and freshness_hours is not None and
                    (now - result_observed).total_seconds() >= freshness_hours * 3600):
                store.enqueue_task("tracking_stale", f"tracking-stale:{order}:{tracking}:{observed_value}", {
                    "order": order, "tracking": tracking or "N/A", "carrier": carrier or "N/A",
                    "reason": "official tracking data is stale", "observed_at": observed_value,
                    "threshold_hours": freshness_hours})
                created.append((order, "tracking_stale")); stale_results += 1

        if not failures:
            store.resolve_tasks("tracking_failure", order, "official tracking recovered")
        if not stale_results:
            store.resolve_tasks("tracking_stale", order, "tracking data is current or shipment is terminal")
        if failures or stale_results:
            store.resolve_tasks("stalled", order, "tracking unavailable or stale; stall decision blocked")
            continue

        carrier = result.get("carrier") or detect_carrier(shipment.get("intl"), shipment.get("carrier"))
        terminal = shipment.get("status") in ("签收", "退回")
        threshold = thresholds.get(carrier)
        changed_at = _datetime(shipment.get("status_observed_at"))
        if threshold is None or changed_at is None or terminal:
            store.resolve_tasks("stalled", order, "stall condition cleared or unavailable")
            continue
        if (now - changed_at).total_seconds() >= threshold * 3600:
            key = f"stalled:{order}:{shipment.get('status_observed_at')}"
            store.enqueue_task("stalled", key, {"order": order, "carrier": carrier,
                "tracking": shipment.get("intl"), "reason": "logistics status has not moved",
                "last_status_at": shipment.get("status_observed_at"), "threshold_hours": threshold})
            created.append((order, "stalled"))
        else:
            store.resolve_tasks("stalled", order, "logistics status moved within threshold")
    return {"created": created, "thresholds": reported,
            "freshness_threshold_hours": freshness_hours if freshness_hours is not None else "N/A"}


def build_daily_report(store, now=None, freshness_hours=None):
    now = now or datetime.now(UTC)
    local_day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    shipments = store.get_shipments()
    results = store.get_document("ups_results", {})
    delivered = []
    for order, shipment in shipments.items():
        if any(event.get("to") == "签收" and _datetime(event.get("at")) and
               _datetime(event.get("at")).astimezone(ZoneInfo("Asia/Shanghai")).date() == local_day
               for event in shipment.get("history") or []):
            delivered.append(order)
    missing = [order for order, shipment in shipments.items() if not shipment.get("intl")]
    unresolved = store.list_tasks(("pending", "retry", "dead", "unknown"))
    freshness = {}
    for result in results.values():
        rows = list((result.get("package_results") or {}).values()) or [result]
        for row in rows:
            carrier = row.get("carrier") or detect_carrier(row.get("tracking")) or "N/A"
            bucket = freshness.setdefault(
                carrier, {"total": 0, "ok": 0, "latest_observed_at": "N/A"})
            bucket["total"] += 1
            bucket["ok"] += int(bool(row.get("ok")))
            observed = row.get("observed_at")
            if observed and (bucket["latest_observed_at"] == "N/A" or
                             observed > bucket["latest_observed_at"]):
                bucket["latest_observed_at"] = observed
    freshness_hours = float(freshness_hours) if freshness_hours not in (None, "") else "N/A"
    return {"date": str(local_day), "denominator": len(shipments),
            "missing_label": {"count": len(missing), "orders": missing},
            "unresolved": {"count": len(unresolved), "task_ids": [row["id"] for row in unresolved]},
            "delivered_today": {"count": len(delivered), "orders": delivered},
            "carrier_freshness": freshness,
            "tracking_data_max_age_hours": freshness_hours,
            "source": "shipments.db + ups_results document"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("refresh", "report"))
    args = parser.parse_args()
    store = Storage()
    if args.command == "refresh":
        configured = {carrier: os.environ.get("STALL_HOURS_" + carrier)
                      for carrier in ("UPS", "DHL", "FEDEX")}
        payload = refresh_operational_tasks(
            store, thresholds=configured,
            freshness_hours=os.environ.get("TRACKING_DATA_MAX_AGE_HOURS"))
    else:
        payload = build_daily_report(
            store, freshness_hours=os.environ.get("TRACKING_DATA_MAX_AGE_HOURS"))
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
