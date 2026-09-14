#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日物流对账报告: 缺面单/在途/异常/签收/未匹配录单人 -> 推群。
用法: python reconcile.py --channel-id <群> [--dry]
"""
import argparse, json, os, re, subprocess, sys, time
import robust
from storage import Storage

ADMIN_UID = (os.environ.get("ADMIN_USER_ID") or "13365")
BOT = (os.environ.get("BOT_APP_ID") or "vbot_EIBezUGncpO8v0QJ")


def operational_issue_counts(store):
    counts = store.task_counts()

    def total(kind, statuses):
        return sum(counts.get(kind, {}).get(status, 0) for status in statuses)

    open_statuses = ("pending", "retry", "running", "unknown")
    return {
        "pipeline_active": total("pipeline", open_statuses),
        "pipeline_dead": total("pipeline", ("dead",)),
        "review": total("review", open_statuses + ("dead",)),
        "tracking": sum(total(kind, open_statuses + ("dead",)) for kind in
                        ("tracking_failure", "tracking_stale")),
        "stalled": total("stalled", open_statuses + ("dead",)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel-id", required=True)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    store = Storage()
    store.migrate_legacy_json()
    db = store.get_shipments()
    res = store.get_document("ups_results", {})
    missing_intl, in_transit, abnormal, delivered, unmatched = [], [], [], [], []
    # 未匹配的非子单: 每日自动重新匹配(新单销售系统同步后即可命中)
    for _o in ([] if a.dry else [k for k, v in db.items() if not v.get("salesperson") and not re.search(r"-\d+$", k)]):
        try:
            subprocess.run([sys.executable, "tracking-pipeline.py", "rematch", "--order", _o],
                           capture_output=True, timeout=60)
        except Exception:
            pass
    db = store.get_shipments()
    for it in db.values():
        order = it.get("orderNo", "")
        is_sub = bool(re.search(r"-\d+$", order))  # 子单(XSD-1)不参与缺面单/未匹配统计
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
    lines.append("累计已签收 %d 单；在途 %d 单；异常 %d 单。" % (len(delivered), len(in_transit), len(abnormal)))
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
    inbox = store.get_inbox(("pending", "retry", "running", "dead"))
    if inbox:
        lines.append("待识别面单 %d 张(OCR 重试中, 已留存文件)" % len(inbox))
    operational = operational_issue_counts(store)
    if operational["pipeline_active"] or operational["pipeline_dead"]:
        lines.append("流水线待执行 %d 项；失败终止 %d 项。" % (
            operational["pipeline_active"], operational["pipeline_dead"]))
    if operational["review"]:
        lines.append("人工审核 %d 项。" % operational["review"])
    if operational["tracking"]:
        lines.append("官网抓取异常待办 %d 项。" % operational["tracking"])
    if operational["stalled"]:
        lines.append("物流停滞待办 %d 项。" % operational["stalled"])
    operational_total = sum(operational.values())
    if not abnormal and not missing_intl and not unmatched and not inbox and not operational_total:
        lines.append("无待处理项。")
    body = "\n".join(lines)
    if a.dry:
        print(body)
        return 0
    # 有问题时额外私信管理员(ADMIN_USER_ID)
    if abnormal or inbox or operational_total:
        dm = ("物流对账待处理：异常 %d 单；待识别面单 %d 张；流水线待执行 %d 项；"
              "流水线终止 %d 项；人工审核 %d 项；官网异常 %d 项；物流停滞 %d 项。") % (
                  len(abnormal), len(inbox), operational["pipeline_active"],
                  operational["pipeline_dead"], operational["review"], operational["tracking"],
                  operational["stalled"])
        robust.cli_run(["im", "+bot-send-user", "--app-id", BOT, "--user-id", ADMIN_UID, "--body", dm])
        subprocess.run([sys.executable, "sendmail.py", "--to", (os.environ.get("EMAIL_TO") or "frank.fu@vertu.cn"),
                        "--subject", "物流对账待处理", "--body", body], capture_output=True)
    rc, out, _ = robust.cli_run(["im", "+agent-notify", "--target", "im", "--agent-slug", "logistics-track",
                                 "--agent-name", "物流小助手", "--bot-name", "物流小助手",
                                 "--channel-id", a.channel_id, "--body", body, "--no-json"])
    print("reconcile rc=%d %s" % (rc, out[:120]))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
