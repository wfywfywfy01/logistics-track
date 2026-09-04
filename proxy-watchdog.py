#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""出口节点看门狗: 每5分钟测代理; s1 挂 -> 切 s2; 都挂超过1小时 -> 私信付汪阳"""
import os, subprocess, sys, time

BASE = "/app/deploy/xray"
MARK = "/app/data/.active_node"
FAIL_LOG = "/app/data/.proxy_fail_count"


def test():
    r = subprocess.run(
        'curl -s -x socks5h://127.0.0.1:10809 --max-time 12 https://ipinfo.io/ip',
        shell=True, capture_output=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def kill_xray():
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            cmd = open("/proc/%s/cmdline" % p, "rb").read().decode(errors="ignore")
        except Exception:
            continue
        if "xray" in cmd and "run" in cmd:
            try:
                os.kill(int(p), 9)
            except Exception:
                pass


def restart_xray():
    kill_xray()
    time.sleep(2)
    subprocess.Popen(["xray", "run", "-c", BASE + "/config.json"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def switch_to(node):
    subprocess.run("cp %s/config-%s.json %s/config.json" % (BASE, node, BASE), shell=True)
    open(MARK, "w").write(node)
    restart_xray()


def main():
    while True:
        time.sleep(300)
        if test():
            try:
                open(FAIL_LOG, "w").write("0")
            except Exception:
                pass
            continue
        node = "s1"
        try:
            node = open(MARK).read().strip() or "s1"
        except Exception:
            pass
        fails = 0
        try:
            fails = int(open(FAIL_LOG).read().strip() or "0")
        except Exception:
            pass
        fails += 1
        open(FAIL_LOG, "w").write(str(fails))
        print("proxy down (node=%s fail=%d)" % (node, fails), flush=True)
        if node == "s1" and fails <= 2:
            print("switch to s2", flush=True)
            switch_to("s2")
            time.sleep(20)
        elif fails % 6 == 0:
            back = "s1" if node == "s2" else "s2"
            print("toggle back to %s" % back, flush=True)
            switch_to(back)
            time.sleep(20)
        if fails >= 12:
            subprocess.run('vertu-cli im +bot-send-user --app-id "vbot_EIBezUGncpO8v0QJ" --user-id "13365" '
                           '--body "物流追踪出口代理两个节点全部不可用, 官网追踪已中断。"',
                           shell=True, capture_output=True)
            subprocess.run('python sendmail.py --to "%s" '
                           '--subject "物流追踪出口代理全部不可用" '
                           '--body "物流追踪出口代理(s1/s2)超过1小时不可用, 官网追踪已中断, 请检查订阅节点。"' % os.environ.get("EMAIL_TO", "frank.fu@vertu.cn"),
                           shell=True, capture_output=True)
            open(FAIL_LOG, "w").write("0")
            print("alert sent", flush=True)


if __name__ == "__main__":
    main()
