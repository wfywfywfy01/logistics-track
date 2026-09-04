#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""异常单(海关扣关/退回)推群警告, ⚠️ 标; customs_alerted 去重"""
import json, os, subprocess, sys
import robust

CHANNEL_ID = os.environ.get("CHANNEL_ID", "e02a0a05-7997-4d50-b0b9-cad9754c0bdc")


def main():
    p = "data/shipments.json"
    with robust.FileLock("data/.ledger.lock"):
        db = robust.load_json_guarded(p, {})
        _sent = _alert_loop(db, p)
    print("alerted:", _sent)
    return

def _alert_loop(db, p):
    sent = 0
    for it in db.values():
        status = it.get("status") or ""
        if status in ("海关扣关", "退回", "异常") and not it.get("customs_alerted"):
            line = ("⚠️【物流异常警告】%s %s｜国际单 %s｜录单人 %s，请跟进处理" % (
                it.get("orderNo"), status, it.get("intl") or "-",
                it.get("salesperson") or "未匹配"))
            cmd = ('vertu-cli im +agent-notify --target im --agent-slug logistics-track '
                   '--agent-name 物流小助手 --bot-name 物流小助手 '
                   '--channel-id "%s" --body "%s"' % (CHANNEL_ID, line.replace('"', "'")))
            r = subprocess.run(cmd, shell=True, capture_output=True)
            print("alert rc=%d %s" % (r.returncode, r.stdout.decode("utf-8", errors="replace")[:120]))
            it["customs_alerted"] = True
            sent += 1
    if sent:
        robust.save_json_guarded(p, db)
    return sent


if __name__ == "__main__":
    main()
