#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自动闭环编排：批量抓 UPS 官网 -> 回填台账 -> 群通知 -> 私聊录单人。
由 logi-watcher.py 在收到新附件/新配对后以后台子进程拉起，互不阻塞。

用法: python auto-track.py --channel-id <群> [--bot-app-id vbot_...] [--skip-track]
"""
import argparse, os, subprocess, sys, time, json
from pathlib import Path
import robust

DATA = Path("data")

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

def main():
    lock = Path("data") / "auto_track.lock"
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip()), 0)  # 进程还活着才跳过
            print("another auto-track running, skip", flush=True)
            return
        except (ValueError, OSError):
            lock.unlink(missing_ok=True)  # 残留锁, 清掉
    lock.write_text(str(os.getpid()))
    try:
        p = argparse.ArgumentParser()
        p.add_argument("--channel-id", required=True)
        p.add_argument("--bot-app-id", default="vbot_EIBezUGncpO8v0QJ")
        p.add_argument("--skip-track", action="store_true", help="跳过官网抓取，只落台账+通知")
        p.add_argument("--mode", default="full", help="full=全量(含签收复查) / incremental=只抓会变动的单")
        a = p.parse_args()

        # 收件箱里的面单图重试 OCR(上次失败/服务器抖动), 成功即移除
        inbox = robust.load_json_guarded("data/inbox.json", [])
        still = []
        for item in inbox:
            item["tries"] = (item.get("tries") or 0) + 1
            img_path = (item.get("path") or "").replace("\\", "/")
            # 本机时代留下的 Windows 路径 -> 统一指向容器 tmp 目录
            if not os.path.exists(img_path):
                img_path = "/app/tmp/" + (item.get("name") or "").replace("\\", "/").split("/")[-1]
            try:
                r = subprocess.run([sys.executable, "ocr_label.py", "--image", img_path],
                                   capture_output=True, timeout=180)
            except subprocess.TimeoutExpired:
                r = None
            d = {}
            if r is not None:
                try:
                    d = json.loads(r.stdout.decode("utf-8", errors="replace"))
                except Exception:
                    d = {}
            if d.get("ok") and d.get("pairs"):
                print("inbox OCR ok:", item.get("name"), d.get("pairs"), flush=True)
            elif item.get("tries", 0) > 6:
                item["gave_up"] = True
                still.append(item)
                print("inbox OCR gave up:", item.get("name"), flush=True)
            else:
                still.append(item)
        robust.atomic_write_json("data/inbox.json", still)
        if not a.skip_track:
            mode = getattr(a, "mode", "full") or "full"
            run(["track_all_ups.py", "--mode", mode], "抓官网")
        run(["wire_results.py"], "回填台账")
        run(["alert.py"], "异常提醒")
        run(["sync_sheet.py"], "同步云表格")
        run(["backup.py"], "台账备份")
        run(["tracking-pipeline.py", "notify",
             "--channel-id", a.channel_id, "--bot-app-id", a.bot_app_id], "群通知")
        run(["send_dms.py"], "私聊录单人")
        # 标记本次已处理当前脏数据, 接管循环不再重复跑
        try:
            dirty = open("data/.dirty").read().strip()
            open("data/.tracked_dirty", "w").write(dirty)
        except Exception:
            pass
        print("AUTO-TRACK DONE", flush=True)
    finally:
        lock.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
