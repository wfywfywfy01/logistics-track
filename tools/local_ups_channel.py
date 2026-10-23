"""本机真人浏览器通道:抓 UPS 官网轨迹并回传台账。

背景:UPS 官网对机房 IP + 自动化浏览器返回 Google reCAPTCHA,容器内抓取失效;
本机 Chrome + 办公网 IP 可以正常拿到数据,抓完再回传服务端应用。

用法:
    python tools/local_ups_channel.py                 # 全流程:导出清单 -> 抓 -> 回传
    python tools/local_ups_channel.py --limit 5       # 只抓 5 单(验证用)
    python tools/local_ups_channel.py --retry-failed  # 连同上次失败的一起重抓
    python tools/local_ups_channel.py --skip-apply    # 只抓,不回传
    python tools/local_ups_channel.py --apply-only    # 只回传已有结果

依赖:DOCKER_HOST(默认 tcp://10.100.0.176:2375)、patchright、本机 Chrome。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK_FILE = REPO / "_local_work_ups.json"
OUT_FILE = REPO / "_local_ups_results.json"
DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://10.100.0.176:2375")
CONTAINER = "logistics-track"

EXPORT_CODE = """
import json, sqlite3
connection = sqlite3.connect("/app/data/shipments.db")
work = []
for order, payload in connection.execute("select order_no, payload from shipments"):
    data = json.loads(payload)
    packages = data.get("packages") or []
    if not packages and data.get("intl"):
        packages = [{"tracking": data.get("intl"), "carrier": data.get("carrier"),
                     "binding_version": data.get("binding_version") or 0}]
    for package in packages:
        tracking = (package.get("tracking") or "").strip()
        if tracking.startswith("1Z"):
            work.append({"order": order, "tracking": tracking,
                         "carrier": package.get("carrier") or "UPS",
                         "status": data.get("status") or "",
                         "binding_version": int(package.get("binding_version") or 0)})
print(json.dumps(work, ensure_ascii=False))
"""


def docker(*args, capture=True):
    env = dict(os.environ, DOCKER_HOST=DOCKER_HOST)
    return subprocess.run(["docker"] + list(args), capture_output=capture,
                          env=env, check=False)


def export_worklist():
    result = docker("exec", "-i", CONTAINER, "python3", "-", capture=True) if False else None
    process = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-"],
        input=EXPORT_CODE.encode("utf-8"), capture_output=True,
        env=dict(os.environ, DOCKER_HOST=DOCKER_HOST), check=False)
    items = json.loads(process.stdout.decode("utf-8"))
    WORK_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    print("导出待查清单: %d 单" % len(items), flush=True)
    return items


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


TERMINAL_STATUSES = ("签收", "退回")


def fetch_pending(work, limit=0, retry_failed=False, refresh_active=False):
    sys.path.insert(0, str(REPO))
    from patchright.sync_api import sync_playwright
    import ups_track

    results = load_json(OUT_FILE, {})
    pending = []
    for item in work:
        previous = results.get(item["tracking"])
        if previous and previous.get("ok"):
            # 每日刷新:已抓成功但台账未到终态的单仍然重抓
            if not (refresh_active and item.get("status") not in TERMINAL_STATUSES):
                continue
        elif previous and not retry_failed:
            continue
        pending.append(item)
    if limit:
        pending = pending[:limit]
    print("本轮待抓 %d 单(缓存 %d)" % (len(pending), len(results)), flush=True)
    if not pending:
        return results

    done = ok = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False, channel="chrome", args=["--window-position=4000,4000"])
        context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 768})
        for item in pending:
            page = context.new_page()
            payloads = []

            def on_response(response, sink=payloads):
                if "GetStatus" in response.url:
                    try:
                        sink.append(response.json())
                    except Exception:
                        pass

            page.on("response", on_response)
            parsed = None
            try:
                page.goto("https://www.ups.com/track?tracknum=%s&loc=en_US" % item["tracking"],
                          timeout=60000, wait_until="domcontentloaded")
                deadline = time.time() + 25
                while time.time() < deadline and not payloads:
                    page.wait_for_timeout(500)
                for payload in payloads:
                    parsed = ups_track.parse_tracking_response(item["tracking"], payload)
                    if parsed:
                        break
            except Exception as error:
                parsed = {"tracking": item["tracking"], "ok": False,
                          "error": type(error).__name__ + ": " + str(error)[:120]}
            page.close()
            if parsed is None:
                parsed = {"tracking": item["tracking"], "ok": False, "error": "no GetStatus data"}
            parsed["order"] = item["order"]
            parsed["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            parsed["binding_version"] = item.get("binding_version", 0)
            parsed["carrier"] = item.get("carrier") or "UPS"
            results[item["tracking"]] = parsed
            done += 1
            ok += int(bool(parsed.get("ok")))
            if done % 3 == 0 or not parsed.get("ok"):
                OUT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
            print("[%d/%d] %s %s -> %s" % (
                done, len(pending), item["order"], item["tracking"],
                parsed.get("stage") or ("ERR: " + str(parsed.get("error"))[:50])), flush=True)
        browser.close()
    OUT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("抓取完成:本轮 %d 单,成功 %d,累计 %d" % (done, ok, len(results)), flush=True)
    return results


def apply_results():
    merge_script = REPO / "tools" / "server_merge_local_results.py"
    docker("cp", str(OUT_FILE), "%s:/tmp/local_ups_results.json" % CONTAINER)
    docker("cp", str(merge_script), "%s:/tmp/merge_local.py" % CONTAINER)
    merged = docker("exec", CONTAINER, "sh", "-c",
                    "cd /app && PYTHONPATH=/app python /tmp/merge_local.py /tmp/local_ups_results.json")
    print("合并:", merged.stdout.decode("utf-8", "replace").strip(), flush=True)
    wired = docker("exec", CONTAINER, "sh", "-c",
                   "cd /app && python wire_results.py 2>&1 | tail -1")
    print("应用:", wired.stdout.decode("utf-8", "replace").strip(), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--skip-apply", action="store_true")
    parser.add_argument("--apply-only", action="store_true")
    parser.add_argument("--refresh-active", action="store_true",
                        help="台账未到终态的单即使抓过也重抓(每日任务用)")
    options = parser.parse_args()

    if not options.apply_only:
        work = export_worklist()
        fetch_pending(work, options.limit, options.retry_failed, options.refresh_active)
    if not options.skip_apply:
        apply_results()


if __name__ == "__main__":
    main()
