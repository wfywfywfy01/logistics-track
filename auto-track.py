#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动闭环编排：批量抓 UPS 官网 -> 回填台账 -> 群通知 -> 私聊录单人。
由 logi-watcher.py 在收到新附件/新配对后以后台子进程拉起，互不阻塞。

用法: python auto-track.py --channel-id <群> [--bot-app-id vbot_...] [--skip-track]
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path
from storage import Storage
import robust

DATA = Path("data")
STORE = Storage(DATA)
STORE.migrate_legacy_json()

def run(args, desc):
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable] + args, capture_output=True,
                           timeout=int(os.environ.get("STEP_TIMEOUT_SECONDS") or 900))
    except subprocess.TimeoutExpired:
        print(f"[{desc}] timed out", flush=True)
        return False
    ok = r.returncode == 0
    out = r.stdout.decode("utf-8", errors="replace").strip()
    print(f"[{desc}] rc={r.returncode} {time.time()-t0:.0f}s", flush=True)
    if out: print("   ", out[-400:], flush=True)
    if not ok:
        print("   ERR:", r.stderr.decode("utf-8", errors="replace")[-300:], flush=True)
    return ok

def execute(a):
    worker = f"auto-track:{os.getpid()}"

    # 租约到期后其他进程可继续处理；新到面单是独立行，不会被旧快照覆盖。
    while True:
        item = STORE.claim_inbox(worker, lease_seconds=240)
        if not item:
            break
        payload = item["payload"]
        img_path = (payload.get("path") or "").replace("\\", "/")
        if not os.path.exists(img_path):
            img_path = "/app/tmp/" + (payload.get("name") or "").replace("\\", "/").split("/")[-1]
        try:
            result = subprocess.run([sys.executable, "ocr_label.py", "--image", img_path],
                                    capture_output=True, timeout=210)
            parsed = json.loads(result.stdout.decode("utf-8", errors="replace"))
            ingested = parsed.get("ingested") or []
            if result.returncode not in (0, 2) or parsed.get("retryable"):
                raise RuntimeError(parsed.get("error") or "OCR produced no fully ingested pair")
            if not parsed.get("pairs") or not ingested or not all(row.get("ok") for row in ingested):
                successful = [row for row in ingested if row.get("ok")]
                failed = [row for row in ingested if not row.get("ok")]
                followups = []
                if successful:
                    followups.append(("pipeline", f"ocr:{item['id']}", {
                        "channel_id": a.channel_id, "bot_app_id": a.bot_app_id}))
                for row in failed:
                    order = row.get("order") or "N/A"
                    tracking = row.get("intl") or "N/A"
                    followups.append(("review", f"pair-review:{order}:{tracking}", {
                        "reason": "OCR pair could not be ingested", "order": order,
                        "intl": tracking, "source_inbox_id": item["id"]}))
                if not followups:
                    followups.append(("review", f"ocr-review:{item['id']}", {
                        "source_inbox_id": item["id"],
                        "name": payload.get("name") or "",
                        "path": img_path,
                        "reason": parsed.get("error") or "OCR produced no fully ingested pair",
                    }))
                STORE.complete_inbox_with_tasks(
                    item["id"], followups, orders=[row.get("order") for row in ingested])
                print("inbox OCR needs review:", payload.get("name"), flush=True)
                continue
            STORE.complete_inbox_with_task(
                item["id"], "pipeline", f"ocr:{item['id']}",
                {"channel_id": a.channel_id, "bot_app_id": a.bot_app_id},
                orders=[row.get("order") for row in ingested])
            print("inbox OCR ok:", payload.get("name"), parsed.get("pairs"), flush=True)
        except Exception as error:
            max_attempts = max(1, int(os.environ.get("OCR_MAX_ATTEMPTS") or 3))
            state = STORE.fail_inbox(item["id"], str(error), max_attempts=max_attempts)
            print("inbox OCR failed:", payload.get("name"), state, str(error)[:120], flush=True)

    tasks = []
    while True:
        task = STORE.claim_task(worker, lease_seconds=1800, kind="pipeline")
        if not task:
            break
        tasks.append(task)
    pending_notifications = (STORE.pending_task_count("notify_group") +
                             STORE.pending_task_count("notify_dm"))
    if a.queued_only and not tasks and not pending_notifications:
        print("no queued pipeline task", flush=True)
        return 0
    if a.queued_only and not tasks:
        args = ["tracking-pipeline.py", "notify", "--channel-id", a.channel_id,
                "--bot-app-id", a.bot_app_id]
        return 0 if run(args, "通知重试") else 1

    steps = []
    if not a.skip_track:
        steps.append((["track_all_ups.py", "--mode", a.mode or "full"], "抓官网", True))
    steps.extend([
        (["wire_results.py"], "回填台账", True),
        (["operations.py", "refresh"], "刷新异常待办", True),
        (["sync_sheet.py"], "同步云表格", False),
        (["backup.py"], "台账备份", True),
        (["tracking-pipeline.py", "notify", "--channel-id", a.channel_id,
          "--bot-app-id", a.bot_app_id], "通知", True),
    ])
    ok = True
    for args, description, required in steps:
        step_ok = run(args, description)
        ok = (step_ok or not required) and ok
    for task in tasks:
        if ok:
            STORE.complete_task(task["id"])
        else:
            STORE.fail_task(task["id"], "pipeline stage failed")
    if not ok:
        print("AUTO-TRACK FAILED", flush=True)
        return 1
    print("AUTO-TRACK DONE", flush=True)
    return 0

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--channel-id", required=True)
    p.add_argument("--bot-app-id", default="vbot_EIBezUGncpO8v0QJ")
    p.add_argument("--skip-track", action="store_true", help="跳过官网抓取，只落台账+通知")
    p.add_argument("--queued-only", action="store_true", help="没有持久任务时直接退出")
    p.add_argument("--mode", default="full", help="full=全量(含签收复查) / incremental=只抓会变动的单")
    options = p.parse_args()
    try:
        with robust.FileLock(DATA / ".auto-track.lock", timeout=1):
            return execute(options)
    except TimeoutError:
        print("auto-track already running", flush=True)
        return 75

if __name__ == "__main__":
    raise SystemExit(main())
