#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日物流对账报告: 缺面单/在途/异常/签收/未匹配录单人 -> 推群。
用法: python reconcile.py --channel-id <群> [--dry]
"""
import argparse, json, os, subprocess, sys, time
from collections import Counter


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel-id", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    db = json.load(open("data/shipments.json", encoding="utf-8"))
    try:
        res = json.load(open("data/ups_results.json", encoding="utf-8"))
    except Exception:
        res = {}
    missing_intl, in_transit, abnormal, delivered, unmatched, awaiting = [], [], [], [], [], []
    import re as _re
    import subprocess as _sp
    # 未匹配的非子单: 每日自动重新匹配(新单销售系统同步后即可命中)
    for _o in [k for k, v in db.items() if not v.get("salesperson") and not _re.search(r"-\d+$", k)]:
        try:
            _sp.run("python tracking-pipeline.py rematch --order %s" % _o,
                    shell=True, capture_output=True, timeout=60)
        except Exception:
            pass
    db = json.load(open("data/shipments.json", encoding="utf-8"))
    for it in db.values():
        order = it.get("orderNo", "")
        is_sub = bool(_re.search(r"-\d+$", order))  # 子单(XSD-1)不参与缺面单/未匹配统计
        intl = it.get("intl") or ""
        status = it.get("status") or "已预报"
        sp = it.get("salesperson") or ""
        if not sp and not is_sub:
            unmatched.append(order)
        if not intl and not is_sub:
            missing_intl.append(order)
            continue
        r = res.get(order) or {}
        stage = r.get("stage") or status
        if stage == "签收":
            delivered.append(order)
        elif stage in ("海关扣关", "退回", "异常"):
            abnormal.append("%s(%s)" % (order, stage))
        else:
            in_transit.append(order)
    lines = ["【物流小助手·每日对账】"]
    lines.append("今日签收 %d 单；在途 %d 单；异常 %d 单。" % (len(delivered), len(in_transit), len(abnormal)))
    if abnormal:
        lines.append("异常：%s" % "、".join(abnormal[:10]))
    if missing_intl:
        lines.append("缺国际面单 %d 单：%s" % (len(missing_intl), "、".join(missing_intl[:12])))
    # 连续抓取失败的单(官网查不到/单号有误)
    fail_orders = [k for k, v in (res or {}).items() if not v.get("ok") and (v.get("fails") or 0) >= 3]
    if fail_orders:
        lines.append("连续抓取失败 %d 单(请核对单号)：%s" % (len(fail_orders), "、".join(fail_orders[:12])))
    if unmatched:
        lines.append("未匹配录单人 %d 单：%s" % (len(unmatched), "、".join(unmatched[:12])))
    try:
        inbox = json.load(open("data/inbox.json", encoding="utf-8"))
    except Exception:
        inbox = []
    if inbox:
        lines.append("待识别面单 %d 张(OCR 重试中, 已留存文件)" % len(inbox))
    if not abnormal and not missing_intl and not unmatched and not inbox:
        lines.append("无待处理项。")
    body = "\n".join(lines)
    if a.dry:
        print(body)
        return
    # 有问题时额外私信付汪阳(13365)
    if abnormal or inbox:
        dm = "物流对账待处理：异常 %d 单；待识别面单 %d 张。" % (len(abnormal), len(inbox))
        subprocess.run('vertu-cli im +bot-send-user --app-id "vbot_EIBezUGncpO8v0QJ" --user-id "13365" --body "%s"' % dm,
                       shell=True, capture_output=True)
        email_to = os.environ.get("EMAIL_TO", "frank.fu@vertu.cn")
        subprocess.run('python sendmail.py --to "%s" --subject "物流对账待处理" --body "%s"' %
                       (email_to, body.replace('"', "'")),
                       shell=True, capture_output=True)
    r = subprocess.run("vertu-cli im +agent-notify --target im --agent-slug logistics-track "
                       "--agent-name 物流小助手 --bot-name 物流小助手 "
                       "--channel-id \"%s\" --body \"%s\"" % (a.channel_id, body.replace('"', "'")),
                       shell=True, capture_output=True)
    out = r.stdout.decode("utf-8", errors="replace")
    print("reconcile rc=%d %s" % (r.returncode, out[:120]))


if __name__ == "__main__":
    main()
