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
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import robust

WORK_FILE = REPO / "_local_work_ups.json"
OUT_FILE = REPO / "_local_ups_results.json"
DOCKER_HOST = os.environ.get("DOCKER_HOST", "tcp://10.100.0.176:2375")
CONTAINER = "logistics-track"

EXPORT_CODE = """
import json, sqlite3
connection = sqlite3.connect("file:/app/data/shipments.db?mode=ro", uri=True)
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
                         "status": package.get("status") or data.get("status") or "",
                         "active": package.get("active", True),
                         "binding_version": int(package.get("binding_version") or 0)})
print(json.dumps(work, ensure_ascii=False))
"""


def docker(*args, capture=True, input=None, timeout=120):
    env = dict(os.environ, DOCKER_HOST=DOCKER_HOST)
    result = subprocess.run(["docker"] + list(args), capture_output=capture,
                            env=env, input=input, timeout=timeout, check=False)
    result.check_returncode()
    return result


def save_json(path, value):
    if not robust.atomic_write_json(path, value):
        raise OSError("Could not atomically save " + str(path))


def export_worklist():
    process = docker("exec", "-i", CONTAINER, "python3", "-", input=EXPORT_CODE.encode("utf-8"))
    items = json.loads(process.stdout.decode("utf-8"))
    save_json(WORK_FILE, items)
    print("导出待查清单: %d 单" % len(items), flush=True)
    return items


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


TERMINAL_STATUSES = ("签收", "退回")


def fetch_pending(work, limit=0, retry_failed=False, refresh_active=False):
    import ups_track

    results = load_json(OUT_FILE, {})
    pending = []
    for item in work:
        if item.get("active") is False or item.get("status") in TERMINAL_STATUSES:
            continue
        previous = results.get(item["tracking"])
        if previous and (previous.get("order") != item["order"] or
                         previous.get("tracking") != item["tracking"] or
                         int(previous.get("binding_version") or 0) != int(item.get("binding_version") or 0)):
            previous = None
        if previous and previous.get("ok"):
            if not refresh_active:
                continue
        elif previous and not (retry_failed or refresh_active):
            continue
        pending.append(item)
    if limit:
        pending = pending[:limit]
    print("本轮待抓 %d 单(缓存 %d)" % (len(pending), len(results)), flush=True)
    if not pending:
        return results

    done = ok = 0
    for item in pending:
        # Reuse the shared response drain and bounded fresh-browser retry logic.
        parsed = ups_track.track_ups(item["tracking"], proxy="", browser_channel="chrome")
        parsed["order"] = item["order"]
        parsed["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        parsed["binding_version"] = item.get("binding_version", 0)
        parsed["carrier"] = item.get("carrier") or "UPS"
        results[item["tracking"]] = parsed
        save_json(OUT_FILE, results)
        done += 1
        ok += int(bool(parsed.get("ok")))
        print("[%d/%d] %s %s -> %s" % (
            done, len(pending), item["order"], item["tracking"],
            parsed.get("stage") or ("ERR: " + str(parsed.get("error"))[:50])), flush=True)
    print("抓取完成:本轮 %d 单,成功 %d,累计 %d" % (done, ok, len(results)), flush=True)
    if not ok:
        raise RuntimeError("UPS local channel returned no successful tracking results")
    return results


def apply_results():
    merge_script = REPO / "tools" / "server_merge_local_results.py"
    docker("cp", str(OUT_FILE), "%s:/tmp/local_ups_results.json" % CONTAINER)
    docker("cp", str(merge_script), "%s:/tmp/merge_local.py" % CONTAINER)
    merged = docker("exec", "-w", "/app", "-e", "PYTHONPATH=/app", CONTAINER,
                    "python", "/tmp/merge_local.py", "/tmp/local_ups_results.json", "--apply", timeout=1800)
    summary = merged.stdout.decode("utf-8", "replace").strip().splitlines()
    print("合并应用:", summary[-1] if summary else "", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--skip-apply", action="store_true")
    parser.add_argument("--apply-only", action="store_true")
    parser.add_argument("--refresh-active", action="store_true",
                        help="台账未到终态的单即使抓过也重抓(每日任务用)")
    options = parser.parse_args()

    with robust.FileLock(OUT_FILE.with_suffix(".lock"), timeout=1):
        if not options.apply_only:
            work = export_worklist()
            fetch_pending(work, options.limit, options.retry_failed, options.refresh_active)
        if not options.skip_apply and (options.apply_only or OUT_FILE.exists()):
            apply_results()


if __name__ == "__main__":
    main()
