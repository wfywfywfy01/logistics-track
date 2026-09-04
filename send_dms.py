import io, json, os, re, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import robust

RES = "data/ups_results.json"; DB = "data/shipments.json"; UM = "data/users_map.json"
BOT = "vbot_EIBezUGncpO8v0QJ"
res = robust.load_json_guarded(RES, {})
db = robust.load_json_guarded(DB, {})
um = robust.load_json_guarded(UM, {})

sent, skipped, failed = [], [], []
for order, r in res.items():
    if not r.get("ok"): continue
    it = db.get(order) or {}
    name = it.get("salesperson") or ""
    # 子单(-N)与无录单人单: 静默跳过(按业务约定不发私聊)
    if not name or re.search(r"-\d+$", order):
        skipped.append(order); continue
    it.setdefault("dm_log", [])
    if it.get("dm_notified_status") == it.get("status"):
        skipped.append(order); continue
    uid = um.get(name)
    if not uid:
        failed.append((order, name, "no uid")); continue
    stage = it.get("status")
    detail = r.get("detail") or r.get("status_en", "")
    dm = "你的订单 %s 物流更新：%s（国际单 %s）%s" % (order, stage, it.get("intl") or "-", detail)
    dm = " ".join(dm.split())  # 必须单行，换行会被截断
    rc, out, err = robust.cli_run(["im", "+bot-send-user", "--app-id", BOT, "--user-id", str(uid), "--body", dm])
    ok = rc == 0 and '"ok": true' in out
    print(("OK  " if ok else "FAIL"), order, name, uid)
    if ok:
        it["dm_notified_status"] = stage
        it["dm_log"].append({"status": stage, "uid": uid, "at": time.strftime("%Y-%m-%d %H:%M")})
        sent.append(order)
    else:
        failed.append((order, name, out[:80]))
    db[order] = it
if sent:
    robust.save_json_guarded(DB, db)
print("sent: %d skipped: %d failed: %d" % (len(sent), len(skipped), len(failed)))
