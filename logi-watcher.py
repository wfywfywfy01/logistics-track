#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""物流小助手 群监听（无 GUI 后台长驻）
轮询群历史：@物流小帮手 + 附件 → xlsx 直接走管线；图片面单入 inbox 待 Agent 视觉处理。
用法: python logi-watcher.py --channel-id e02a0a05-... [--bot-app-id vbot_...] [--interval 30] [--once]
"""
import argparse, json, os, re, subprocess, sys, time
from pathlib import Path

DATA = Path("data"); DATA.mkdir(exist_ok=True)
STATE = DATA / "watcher_state.json"; INBOX = DATA / "inbox.json"
TMP = Path("tmp"); TMP.mkdir(exist_ok=True)
AGENT_BOT_ID = os.environ.get("AGENT_BOT_ID", "886e0664-78dd-4e58-af82-17b35ebe85c2")  # 专家 bot, @ 它时引导回复

ORDER_RE = re.compile(r"\b((?:XSD|CKD)[-\w]+)\b", re.I)
INTL_RE = re.compile(r"\b(1Z[A-Z0-9]{10,18}|[A-Z]{2}\d{8,14}|\d{9,14})\b", re.I)
# 一行内的显式配对: XSD...==1Z...  /  XSD...｜1Z...  /  XSD... 1Z...
PAIR_RE = re.compile(r"((?:XSD|CKD)[-\w]+)\s*(?:==|=|｜|\||\s)\s*(1Z[A-Z0-9]{10,18}|[A-Z]{2}\d{8,14}|\d{9,14})", re.I)

def cli(args):
    r = subprocess.run("vertu-cli " + " ".join(f'"{a}"' for a in args), shell=True, capture_output=True)
    return r.stdout.decode("utf-8", errors="replace") if r.returncode == 0 else None

def cli_json(args):
    out = cli(args)
    try: return json.loads(out) if out else None
    except Exception: return None

def load(p, d):
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception: pass
    return d
def save(p, o): p.write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")

def process_attachment(att, channel_id, bot_app_id):
    name = att.get("name", "file")
    url = att.get("url")
    if not url: return None
    # 同名字附件防撞车: 用 URL 尾部 uuid 段做前缀
    uid = re.sub(r"[^A-Za-z0-9-]", "", url)[-12:] or name
    target = TMP / (uid + "_" + name)
    # 已下载过(文件存在且非空)就直接复用, +attachment-download 对已存在文件会报错
    if not (target.exists() and target.stat().st_size > 0):
        out = cli(["im", "+attachment-download", "--url", f'"{url}"', "--output", str(target).replace("\\", "/"), "--no-json"])
        if out is None or not target.exists(): return {"name": name, "ok": False}
    lower = name.lower()
    if lower.endswith((".xlsx", ".xls")):
        r = subprocess.run([sys.executable, "tracking-pipeline.py", "ingest-forecast", "--file", str(target)],
                           capture_output=True)
        try: res = json.loads(r.stdout.decode("utf-8", errors="replace"))
        except Exception: res = {"raw": r.stdout.decode("utf-8", errors="replace")[:200]}
        return {"name": name, "ok": True, "kind": "forecast", "result": res}
    if lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
        # OCR 不阻塞消息队列: 图片一律先进收件箱, 由 auto-track 的收件箱重试(带超时)处理
        inbox = load(INBOX, [])
        inbox.append({"url": url, "name": name, "path": str(target), "at": time.strftime("%Y-%m-%d %H:%M")})
        save(INBOX, inbox)
        return {"name": name, "ok": True, "kind": "label"}
    return {"name": name, "ok": True, "kind": "ignored"}

def extract_pairs(body):
    """从消息正文提取 (订单号, 国际单) 配对。
    优先逐行显式配对 XSD…==1Z…；同一行/同一条消息内多个订单号与多个单号则按出现顺序配对。"""
    pairs, seen = [], set()
    for line in (body or "").splitlines():
        for m in PAIR_RE.finditer(line):
            o, t = m.group(1).upper(), m.group(2).upper()
            if (o, t) not in seen:
                seen.add((o, t)); pairs.append((o, t))
        if PAIR_RE.search(line):
            continue
        os_, ts_ = ORDER_RE.findall(line), INTL_RE.findall(line)
        if os_ and ts_ and len(os_) == len(ts_):
            for o, t in zip(os_, ts_):
                o, t = o.upper(), t.upper()
                if (o, t) not in seen:
                    seen.add((o, t)); pairs.append((o, t))
    # 整条消息兜底：订单数与单号数相等则顺序配对
    if not pairs:
        os_, ts_ = ORDER_RE.findall(body or ""), INTL_RE.findall(body or "")
        if os_ and ts_ and len(os_) == len(ts_):
            pairs = list(zip([x.upper() for x in os_], [x.upper() for x in ts_]))
    return pairs

def process_text(body, channel_id):
    """文字里的 XSD==1Z 配对 -> ingest-pair。返回处理结果列表。"""
    out = []
    for order, intl in extract_pairs(body):
        r = subprocess.run([sys.executable, "tracking-pipeline.py", "ingest-pair",
                            "--order", order, "--intl", intl], capture_output=True)
        ok = r.returncode == 0
        txt = r.stdout.decode("utf-8", errors="replace").strip()
        out.append({"kind": "pair", "order": order, "intl": intl, "ok": ok,
                  "result": txt[:200].encode("ascii", "replace").decode()})
    return out

def spawn_auto_track(channel_id, bot_app_id, skip_track=False):
    """后台拉起自动闭环，不阻塞 watch 循环。"""
    args = [sys.executable, "auto-track.py", "--mode", "incremental", "--channel-id", channel_id, "--bot-app-id", bot_app_id]
    if skip_track: args.append("--skip-track")
    kwargs = {"cwd": str(Path(__file__).resolve().parent)}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(args, **kwargs)
    print("spawned auto-track", flush=True)
def watch_once(channel_id, bot_app_id, since_ts):
    data = cli_json(["im", "+history", "--channel-id", channel_id, "--limit", "50"])
    msgs = (data or {}).get("messages", [])
    results, latest = [], since_ts
    for m in sorted(msgs, key=lambda x: x.get("created_at", "")):
        ts = m.get("created_at", "")
        if ts <= since_ts: continue
        # 不处理机器人自己发的消息(通知/ack/警告), 防止自循环
        if m.get("sender_type") == "bot" or m.get("sender_bot_id"):
            latest = max(latest, ts)
            continue
        msg_items = []
        for att in m.get("attachments", []):
            if att.get("attachment_type") in ("file", "image"):
                msg_items.append(process_attachment(att, channel_id, bot_app_id))
        msg_items.extend(process_text(m.get('body') or '', channel_id))
        results.extend(msg_items)
        # @ 了专家 bot 但没有任何可执行内容 -> 引导回复
        body_low = (m.get("body") or "").lower()
        mentions = ((m.get("metadata") or {}).get("mentions") or [])
        mentioned = any(x.get("type") == "bot" and x.get("bot_id") == AGENT_BOT_ID for x in mentions) or any(k in body_low for k in ("@物流小助手", "@hermes logistics-track", "@物流追踪机器人"))
        # 有附件但下载失败: 重试计数, 超过3次放弃并推进游标(不堵后面的消息)
        failed = [r for r in msg_items if r and not r.get("ok")]
        if failed:
            mid = m.get("id", "")
            dl_fails = load(DATA / ".dl_fails.json", {})
            n = dl_fails.get(mid, 0) + 1
            if n < 3:
                dl_fails[mid] = n
                save(DATA / ".dl_fails.json", dl_fails)
                break
            dl_fails.pop(mid, None)
            save(DATA / ".dl_fails.json", dl_fails)
            print("give up on attachment:", [r.get("name") for r in failed], flush=True)
        latest = max(latest, ts)
        if mentioned and not [r for r in msg_items if r]:
            cli(["im", "+agent-notify", "--target", "im", "--agent-slug", "logistics-track",
                 "--agent-name", "物流小助手", "--bot-name", "物流小助手",
                 "--channel-id", channel_id, "--no-json",
                 "--body", "【物流小助手】在的！把预报 xlsx、面单图片发到群里，或直接发文字配对（XSD…==1Z…），我就会自动查官网轨迹、通知录单人。"])
    done = [r for r in results if r and r.get("ok")]
    if done:
        n_x = sum(1 for r in done if r.get("kind") == "forecast")
        n_p = sum(1 for r in done if r.get("kind") == "label")
        n_pair = sum(1 for r in done if r.get("kind") == "pair")
        n_ocr = sum(1 for r in done if r.get("kind") == "label_ocr")
        parts = ([f"预报xlsx {n_x} 份已聚类"] if n_x else []) + ([f"面单OCR {n_ocr} 张已配对"] if n_ocr else []) + ([f"面单 {n_p} 张待识别"] if n_p else []) + ([f"配对 {n_pair} 单"] if n_pair else [])
        cli(["im", "+agent-notify", "--target", "im", "--agent-slug", "logistics-track",
             "--agent-name", "物流小助手", "--bot-name", "物流小助手",
             "--channel-id", channel_id, "--no-json",
             "--body", "【物流小助手】已处理本批：" + "；".join(parts) + "。正在抓取官网轨迹…"])
    if done:
        # 崩溃安全: 写脏标记, 入口的接管循环会保证抓取一定被执行
        open(str(DATA / ".dirty"), "w").write(str(time.time()))
        if not os.environ.get("LOGI_NO_AUTOTRACK"):
            spawn_auto_track(channel_id, bot_app_id)
    return results, latest

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--channel-id", required=True)
    p.add_argument("--bot-app-id")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    st = load(STATE, {})
    since = st.get("since", "2000-01-01T00:00:00Z")
    while True:
        try:
            results, since = watch_once(args.channel_id, args.bot_app_id, since)
            save(STATE, {"since": since})
            for r in results:
                if r: print(json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as e:
            print("watcher error:", e, file=sys.stderr, flush=True)
        if args.once: break
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
