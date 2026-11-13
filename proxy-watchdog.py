#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每5分钟探测活动代理并切换节点；连续失败12次告警一次，成功后重新计数。"""
import os, shutil, subprocess, sys, time
import robust

ADMIN_UID = (os.environ.get("ADMIN_USER_ID") or "13365")
EMAIL_TO = (os.environ.get("EMAIL_TO") or "frank.fu@vertu.cn")

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
    """切到指定节点; 该节点配置不存在(未配 XRAY2_*)则只重启当前 xray"""
    src = "%s/config-%s.json" % (BASE, node)
    if os.path.exists(src):
        shutil.copyfile(src, BASE + "/config.json")
        open(MARK, "w").write(node)
    else:
        print("no config for %s, restart current" % node, flush=True)
    restart_xray()


def alert_failure(node, fails):
    body = ("物流追踪出口代理探测连续失败 %d 次：探测节点 %s 经本地代理访问 "
            "https://ipinfo.io/ip 未成功。请检查代理连通性或探测目标可用性。") % (fails, node)
    try:
        rc, _, _ = robust.cli_run([
            "im", "+bot-send-user", "--app-id", "vbot_EIBezUGncpO8v0QJ", "--user-id", ADMIN_UID,
            "--body", body])
        print("proxy alert im attempt rc=%s" % rc, flush=True)
    except Exception as error:
        print("proxy alert im attempt failed: " + type(error).__name__, flush=True)
    try:
        result = subprocess.run([sys.executable, "sendmail.py", "--to", EMAIL_TO,
                                 "--subject", "物流追踪出口代理探测持续失败", "--body", body],
                                capture_output=True, timeout=120)
        print("proxy alert email attempt rc=%s" % result.returncode, flush=True)
    except Exception as error:
        print("proxy alert email attempt failed: " + type(error).__name__, flush=True)


def check_once():
    healthy = test()
    with robust.FileLock(FAIL_LOG + ".lock"):
        if healthy:
            fails = 0
        else:
            try:
                with open(FAIL_LOG) as handle:
                    fails = int(handle.read().strip() or "0")
            except FileNotFoundError:
                fails = 0
            fails += 1
        # Consume the one alert attempt before external sends. A crash or ambiguous
        # receipt must not replay it; only a successful probe resets suppression.
        if not robust.atomic_write_json(FAIL_LOG, fails):
            raise OSError("could not persist proxy failure state")
    if healthy:
        return
    try:
        with open(MARK) as handle:
            node = handle.read().strip() or "s1"
    except OSError:
        node = "s1"
    print("proxy probe failed (node=%s fail=%d)" % (node, fails), flush=True)
    try:
        if node == "s1" and fails <= 2:
            print("switch to s2", flush=True)
            switch_to("s2")
            time.sleep(20)
        elif fails % 6 == 0:
            back = "s1" if node == "s2" else "s2"
            print("toggle back to %s" % back, flush=True)
            switch_to(back)
            time.sleep(20)
    except Exception as error:
        print("proxy switch failed: " + type(error).__name__, flush=True)
    if fails == 12:
        alert_failure(node, fails)


def main():
    while True:
        time.sleep(300)
        try:
            check_once()
        except Exception as error:
            print("watchdog check failed: " + type(error).__name__, flush=True)


if __name__ == "__main__":
    main()
