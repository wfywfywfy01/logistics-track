#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""台账 -> 云文档智能表格 同步。
用法: python sync_sheet.py
环境: SHEET_DOC_ID (默认物流追踪表), 表格列 A-I
"""
import json, os, re, tempfile, time
import robust

DOC_ID = (os.environ.get("SHEET_DOC_ID") or "3e553a37-880d-4c72-873a-fa7caa3aef9c")
HEADERS = ["订单号", "录单人", "国际单号", "承运商", "顺丰单号", "产品", "状态", "最新节点", "签收时间", "更新时间"]


def col_letter(i):
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    db = robust.load_json_guarded("data/shipments.json", {})
    res = robust.load_json_guarded("data/ups_results.json", {})
    rows = sorted(db.values(), key=lambda x: x.get("orderNo", ""))
    cells = []
    for ci, h in enumerate(HEADERS):
        cells.append({"cell": "%s1" % col_letter(ci), "value": h})
    now = time.strftime("%Y-%m-%d %H:%M")
    for ri, it in enumerate(rows, start=2):
        order = it.get("orderNo", "")
        r = res.get(order) or {}
        intl = it.get("intl") or ""
        if it.get("alt_intl") and it.get("alt_intl") != intl:
            intl = intl + " / " + it.get("alt_intl")
        carrier = r.get("carrier") or ("UPS" if (it.get("intl") or "").startswith("1Z") else ("DHL" if it.get("intl") else ""))
        detail = r.get("detail") or ""
        if not detail and it.get("history"):
            detail = (it["history"][-1] or {}).get("detail", "")
        delivered_at = ""
        if (r.get("stage") or it.get("status")) == "签收":
            d = r.get("detail") or ""
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*(A\.M\.|P\.M\.)", d, re.I)
            if m:
                mm, dd, yyyy, hh, mi, ap = m.groups()
                h = int(hh)
                if ap.upper().startswith("P") and h != 12:
                    h += 12
                if ap.upper().startswith("A") and h == 12:
                    h = 0
                delivered_at = "%s-%s-%s %02d:%s" % (yyyy, mm, dd, h, mi)
            else:
                m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", d)
                if m2:
                    delivered_at = "%s-%s-%s" % m2.groups()
        vals = [
            order,
            it.get("salesperson") or "未匹配",
            intl,
            carrier,
            it.get("domestic") or "",
            ((it.get("products") or [""])[0] or "")[:60],
            it.get("status") or "已预报",
            (detail or "")[:60],
            delivered_at,
            now,
        ]
        for ci, v in enumerate(vals):
            cells.append({"cell": "%s%d" % (col_letter(ci), ri), "value": v or ""})
    # 清掉旧的残留行
    for ri in range(len(rows) + 2, len(rows) + 60):
        cells.append({"cell": "A%d" % ri, "value": None})
    fd, path = tempfile.mkstemp(suffix=".json", prefix="sheetupd_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cells, f, ensure_ascii=False)
    try:
        rc, out, _ = robust.cli_run(["docs", "+sheet-set-cells", "--doc-id", DOC_ID, "--updates-file", path, "--no-json"])
        print("sheet sync rc=%d %s" % (rc, out[:150]))
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
