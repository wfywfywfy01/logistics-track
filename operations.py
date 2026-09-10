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


def refresh_operational_tasks(store, now=None, thresholds=None):
    now = (now or datetime.now(UTC)).astimezone(UTC)
    thresholds = {str(k).upper(): float(v) for k, v in (thresholds or {}).items() if v not in (None, "")}
    reported = {carrier: thresholds.get(carrier, "N/A") for carrier in ("UPS", "DHL", "FEDEX")}
    shipments = store.get_shipments()
    results = store.get_document("ups_results", {})
    created = []
    for order, shipment in shipments.items():
        if not shipment.get("intl") and not shipment.get("packages"):
            continue
        result = results.get(order) or {}
        carrier = result.get("carrier") or detect_carrier(shipment.get("intl"), shipment.get("carrier"))
        if not result.get("ok"):
            observed = result.get("observed_at") or "N/A"
            store.enqueue_task("tracking_failure", f"tracking-failure:{order}:{observed}", {
                "order": order, "tracking": shipment.get("intl"), "carrier": carrier or "N/A",
                "reason": "official tracking failed", "observed_at": observed,
                "error": result.get("error") or "N/A"})
            created.append((order, "tracking_failure"))
            store.resolve_tasks("stalled", order, "tracking unavailable; stall decision blocked")
            continue
        store.resolve_tasks("tracking_failure", order, "official tracking recovered")
        threshold = thresholds.get(carrier)
        changed_at = _datetime(shipment.get("status_observed_at"))
        terminal = shipment.get("status") in ("签收", "退回")
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
    return {"created": created, "thresholds": reported}


def build_daily_report(store, now=None):
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
    for order, result in results.items():
        carrier = result.get("carrier") or detect_carrier(result.get("tracking")) or "N/A"
        bucket = freshness.setdefault(carrier, {"total": 0, "ok": 0, "latest_observed_at": "N/A"})
        bucket["total"] += 1
        bucket["ok"] += int(bool(result.get("ok")))
        observed = result.get("observed_at")
        if observed and (bucket["latest_observed_at"] == "N/A" or observed > bucket["latest_observed_at"]):
            bucket["latest_observed_at"] = observed
    return {"date": str(local_day), "denominator": len(shipments),
            "missing_label": {"count": len(missing), "orders": missing},
            "unresolved": {"count": len(unresolved), "task_ids": [row["id"] for row in unresolved]},
            "delivered_today": {"count": len(delivered), "orders": delivered},
            "carrier_freshness": freshness,
            "source": "shipments.db + ups_results document"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("refresh", "report"))
    args = parser.parse_args()
    store = Storage()
    if args.command == "refresh":
        configured = {carrier: os.environ.get("STALL_HOURS_" + carrier)
                      for carrier in ("UPS", "DHL", "FEDEX")}
        payload = refresh_operational_tasks(store, thresholds=configured)
    else:
        payload = build_daily_report(store)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
