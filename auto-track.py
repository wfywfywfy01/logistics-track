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
    r = subprocess.run([sys.executable] + args, capture_output=True)
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
            if result.returncode != 0 or not parsed.get("pairs") or not ingested or not all(
                    row.get("ok") for row in ingested):
                raise RuntimeError(parsed.get("error") or "OCR produced no fully ingested pair")
            STORE.complete_inbox(item["id"])
            STORE.enqueue_task("pipeline", f"ocr:{item['id']}",
                               {"channel_id": a.channel_id, "bot_app_id": a.bot_app_id})
            print("inbox OCR ok:", payload.get("name"), parsed.get("pairs"), flush=True)
        except Exception as error:
            state = STORE.fail_inbox(item["id"], str(error))
            print("inbox OCR failed:", payload.get("name"), state, str(error)[:120], flush=True)

    tasks = []
    while True:
        task = STORE.claim_task(worker, lease_seconds=1800, kind="pipeline")
        if not task:
            break
        tasks.append(task)
    if a.queued_only and not tasks:
        print("no queued pipeline task", flush=True)
        return 0

    steps = []
    if not a.skip_track:
        steps.append((["track_all_ups.py", "--mode", a.mode or "full"], "抓官网"))
    steps.extend([
        (["wire_results.py"], "回填台账"),
        (["alert.py"], "异常提醒"),
        (["sync_sheet.py"], "同步云表格"),
        (["backup.py"], "台账备份"),
        (["tracking-pipeline.py", "notify", "--channel-id", a.channel_id,
          "--bot-app-id", a.bot_app_id], "群通知"),
        (["send_dms.py"], "私聊录单人"),
    ])
    ok = True
    for args, description in steps:
        step_ok = run(args, description)
        ok = step_ok and ok
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
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
