"""Merge fresh local UPS snapshots without replacing other package results."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json

import robust
from official_tracking import result_hash, result_matches_package
from storage import Storage
from track_retry import _rollup
from wire_results import apply_results


def observed_at(value):
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation requires a timezone")
    return parsed.astimezone(timezone.utc)


def merge_results(store, incoming):
    if not isinstance(incoming, dict):
        raise ValueError("local results must be an object keyed by tracking number")
    counts = Counter()
    accepted_orders = set()
    latest_allowed = datetime.now(timezone.utc) + timedelta(minutes=5)

    def merge(document):
        document = dict(document or {})
        # Read bindings after mutate_document holds the SQLite write transaction.
        ledger = store.get_shipments()
        for tracking, row in incoming.items():
            if (not isinstance(row, dict) or row.get("tracking") != tracking or
                    row.get("ok") is not True or row.get("source") != "ups.com" or
                    not isinstance(row.get("events"), list) or len(row["events"]) > 1000 or
                    any(not isinstance(event, dict) for event in row["events"])):
                counts["invalid_or_failed"] += 1
                continue
            order = row.get("order")
            shipment = ledger.get(order) or {}
            declared = shipment.get("packages") or [
                {**shipment, "tracking": number} for number in
                (shipment.get("intl"), shipment.get("alt_intl")) if number]
            active = {item.get("tracking"): item for item in declared
                      if item.get("tracking") and item.get("active") is not False}
            bound = active.get(tracking)
            if not result_matches_package(row, bound):
                counts["stale_binding"] += 1
                continue
            previous = document.get(order) or {}
            packages = dict(previous.get("package_results") or {})
            if not packages and previous.get("tracking"):
                packages[previous["tracking"]] = previous
            cached = packages.get(tracking) or {}
            if not result_matches_package(cached, bound):
                cached = {}
            snapshot = bound.get("official_tracking") or {}
            try:
                observed = observed_at(row.get("observed_at"))
                if observed > latest_allowed:
                    counts["future_observation"] += 1
                    continue
                watermarks = [observed_at(value) for value in (
                    cached.get("observed_at"), snapshot.get("observed_at"),
                    bound.get("status_observed_at"), bound.get("observation_observed_at"),
                    (bound.get("last_observation") or {}).get("observed_at")) if value]
                if watermarks and observed < max(watermarks):
                    counts["stale_observation"] += 1
                    continue
                fingerprint = result_hash(row)
                conflicts = [(cached.get("observed_at"), result_hash(cached)),
                             (snapshot.get("observed_at"), snapshot.get("result_hash"))]
                if any(timestamp and digest and observed == observed_at(timestamp) and
                       digest != fingerprint for timestamp, digest in conflicts):
                    counts["conflicting_observation"] += 1
                    continue
            except (TypeError, ValueError):
                counts["invalid_observation"] += 1
                continue
            accepted_orders.add(order)
            if cached == row and previous.get("package_results"):
                counts["unchanged"] += 1
                continue
            packages[tracking] = row
            # Keep all existing siblings; only current bindings count toward rollup.
            rows = [packages.get(number) if result_matches_package(packages.get(number), package)
                    else {} for number, package in active.items()]
            primary = packages.get(next(iter(active))) or row
            complete = all(item.get("ok") for item in rows)
            document[order] = {**primary, "order": order, "package_results": packages,
                "ok": complete, "partial": any(item.get("ok") for item in rows) and not complete,
                "stage": _rollup(rows)}
            counts["merged"] += 1
        return document

    store.mutate_document("ups_results", merge, {})
    return {"incoming": len(incoming), "merged": counts["merged"],
            "accepted_orders": sorted(accepted_orders),
            "skipped": {key: value for key, value in counts.items() if key != "merged"}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="/tmp/local_ups_results.json")
    parser.add_argument("--apply", action="store_true", help="apply accepted orders under the batch lock")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8-sig") as source:
        incoming = json.load(source)
    store = Storage()
    with robust.FileLock(store.data_dir / ".auto-track.lock", timeout=30):
        outcome = merge_results(store, incoming)
        orders = set(outcome.pop("accepted_orders"))
        if args.apply:
            outcome["apply"] = apply_results(store, orders=orders)
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if outcome.get("apply", {}).get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
