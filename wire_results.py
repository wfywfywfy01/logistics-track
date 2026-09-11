#!/usr/bin/env python
"""Apply official carrier results to package and order state."""
import subprocess
import sys

from storage import Storage


def run_pipeline(args):
    result = subprocess.run([sys.executable, "tracking-pipeline.py"] + args,
                            capture_output=True, timeout=120)
    return result.returncode, result.stdout.decode("utf-8", errors="replace").strip()


def apply_results(store, runner=run_pipeline):
    results = store.get_document("ups_results", {})
    sales = store.get_document("sales_map", {})
    ledger = store.get_shipments()
    applied = failed = 0
    for order, result in results.items():
        if not result.get("ok") and not (result.get("package_results") or {}):
            continue
        tracking = result.get("tracking") or ledger.get(order, {}).get("intl") or (sales.get(order) or {}).get("intl")
        if order not in ledger and tracking:
            code, output = runner(["ingest-pair", "--order", order, "--intl", tracking])
            print("ingest:", order, "->", output[:100], flush=True)
            failed += int(code != 0)
        package_results = result.get("package_results") or {}
        if package_results:
            for package in package_results.values():
                if not package.get("ok"):
                    continue
                args = ["package-update", "--order", order,
                        "--tracking", package.get("tracking") or "",
                        "--status", package["stage"],
                        "--detail", package.get("detail") or package.get("status_en", ""),
                        "--observed-at", package.get("observed_at") or ""]
                if package.get("binding_version") is not None:
                    args += ["--binding-version", str(package["binding_version"])]
                code, output = runner(args)
                print("package-update:", order, package.get("tracking"), "->", output[:120], flush=True)
                applied += int(code == 0); failed += int(code != 0)
            continue
        if not result.get("ok"):
            print("skip (not ok):", order, flush=True)
            continue
        args = ["track-update", "--order", order, "--status", result["stage"],
                "--detail", result.get("detail") or result.get("status_en", ""),
                "--tracking", result.get("tracking") or "",
                "--observed-at", result.get("observed_at") or ""]
        if result.get("binding_version") is not None:
            args += ["--binding-version", str(result["binding_version"])]
        code, output = runner(args)
        print("update:", order, result["stage"], "->", output[:120], flush=True)
        applied += int(code == 0); failed += int(code != 0)
    return {"applied": applied, "failed": failed}


if __name__ == "__main__":
    outcome = apply_results(Storage())
    print("ALL DONE", outcome, flush=True)
    raise SystemExit(1 if outcome["failed"] else 0)
