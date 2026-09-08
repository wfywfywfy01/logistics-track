#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""物流小助手 群监听（无 GUI 后台长驻）
轮询群历史：@物流小帮手 + 附件 → xlsx 直接走管线；图片面单入 inbox 待 Agent 视觉处理。
用法: python logi-watcher.py --channel-id e02a0a05-... [--bot-app-id vbot_...] [--interval 30] [--once]
"""
import argparse, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
import robust
from storage import Storage

DATA = Path("data"); DATA.mkdir(exist_ok=True)
STATE = DATA / "watcher_state.json"; INBOX = DATA / "inbox.json"
TMP = Path("tmp"); TMP.mkdir(exist_ok=True)
STORE = Storage(DATA)
STORE.migrate_legacy_json()
AGENT_BOT_ID = (os.environ.get("AGENT_BOT_ID") or "886e0664-78dd-4e58-af82-17b35ebe85c2")  # 专家 bot, @ 它时引导回复

ORDER_RE = re.compile(r"\b((?:XSD|CKD)[-\w]+)\b", re.I)
INTL_RE = re.compile(r"\b(1Z[A-Z0-9]{10,18}|[A-Z]{2}\d{8,14}|\d{9,14})\b", re.I)
# 一行内的显式配对: XSD...==1Z...  /  XSD...｜1Z...  /  XSD... 1Z...
PAIR_RE = re.compile(r"((?:XSD|CKD)[-\w]+)\s*(?:==|=|｜|\||\s)\s*(1Z[A-Z0-9]{10,18}|[A-Z]{2}\d{8,14}|\d{9,14})", re.I)

def cli(args):
    try:
        rc, out, _ = robust.cli_run(args)
    except Exception:
        return None
    return out if rc == 0 else None

def cli_json(args):
    out = cli(args)
    try: return json.loads(out) if out else None
    except Exception: return None

def load(p, d): return STORE.get_document(p.stem.lstrip("."), d)
def save(p, o): STORE.put_document(p.stem.lstrip("."), o)

def process_attachment(att, channel_id, bot_app_id, item_id=None):
    name = Path(att.get("name", "file")).name
    url = att.get("url")
    if not url: return None
    # 同名字附件防撞车: 用 URL 尾部 uuid 段做前缀
    uid = re.sub(r"[^A-Za-z0-9-]", "", url)[-12:] or name
    target = TMP / (uid + "_" + name)
    # 已下载过(文件存在且非空)就直接复用, +attachment-download 对已存在文件会报错
    if not (target.exists() and target.stat().st_size > 0):
        out = cli(["im", "+attachment-download", "--url", url, "--output", str(target).replace("\\", "/"), "--no-json"])
        if out is None or not target.exists(): return {"name": name, "ok": False}
    max_bytes = int(os.environ.get("ATTACHMENT_MAX_BYTES") or str(20 * 1024 * 1024))
    if target.stat().st_size > max_bytes:
        target.unlink(missing_ok=True)
        return {"name": name, "ok": False, "error": "attachment exceeds size limit"}
    lower = name.lower()
    signature = target.read_bytes()[:12]
    if lower.endswith(".xlsx"):
        if not signature.startswith(b"PK"):
            return {"name": name, "ok": False, "error": "invalid xlsx content"}
        r = subprocess.run([sys.executable, "tracking-pipeline.py", "ingest-forecast", "--file", str(target)],
                           capture_output=True)
        if r.returncode != 0:
            return {"name": name, "ok": False, "kind": "forecast",
                    "error": r.stderr.decode("utf-8", errors="replace")[:200]}
        try: res = json.loads(r.stdout.decode("utf-8", errors="replace"))
        except Exception: res = {"raw": r.stdout.decode("utf-8", errors="replace")[:200]}
        ok = bool(res.get("ingested"))
        return {"name": name, "ok": ok, "kind": "forecast", "result": res,
                **({} if ok else {"error": "forecast contains no valid orders"})}
    if lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
        is_image = (signature.startswith(b"\x89PNG\r\n\x1a\n") or
                    signature.startswith(b"\xff\xd8\xff") or
                    (signature.startswith(b"RIFF") and signature[8:12] == b"WEBP"))
        if not is_image:
            return {"name": name, "ok": False, "error": "invalid image content"}
        # 图片先持久入队，OCR worker 用租约领取；重启和并发新到件都不会覆盖。
        item_id = item_id or hashlib.sha256(url.encode()).hexdigest()
        STORE.enqueue_inbox(item_id, {"url": url, "name": name, "path": str(target)})
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


def fetch_history(channel_id, since_ts):
    """Fetch every page since the persisted high-water time; IDs provide overlap dedupe."""
    page_size = 50
    max_pages = int(os.environ.get("HISTORY_MAX_PAGES") or "100")
    latest = since_ts
    for page in range(max_pages):
        args = ["im", "+history", "--channel-id", channel_id,
                "--date-from", since_ts, "--limit", str(page_size),
                "--offset", str(page * page_size)]
        data = cli_json(args)
        if data is None:
            raise RuntimeError("history query failed")
        messages = (data or {}).get("messages", [])
        for message in messages:
            message_id = message.get("id") or "sha256:" + hashlib.sha256(
                json.dumps(message, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            message["id"] = message_id
            STORE.record_message(channel_id, message_id,
                                 message.get("created_at") or since_ts, message)
            latest = max(latest, message.get("created_at") or since_ts)
        if len(messages) < page_size:
            return latest
    raise RuntimeError("history pagination limit reached; increase HISTORY_MAX_PAGES")


def watch_once(channel_id, bot_app_id, since_ts):
    latest = fetch_history(channel_id, since_ts)
    results, should_run = [], False
    for stored in STORE.pending_messages(channel_id):
        m = stored["payload"]
        message_id = stored["id"]
        # 不处理机器人自己发的消息(通知/ack/警告), 防止自循环
        if m.get("sender_type") == "bot" or m.get("sender_bot_id"):
            STORE.complete_message(channel_id, message_id)
            continue
        msg_items = []
        for index, att in enumerate(m.get("attachments", [])):
            if att.get("attachment_type") in ("file", "image"):
                msg_items.append(process_attachment(
                    att, channel_id, bot_app_id, f"{message_id}:{index}"
                ))
        msg_items.extend(process_text(m.get('body') or '', channel_id))
        results.extend(msg_items)
        # @ 了专家 bot 但没有任何可执行内容 -> 引导回复
        body_low = (m.get("body") or "").lower()
        mentions = ((m.get("metadata") or {}).get("mentions") or [])
        mentioned = any(x.get("type") == "bot" and x.get("bot_id") == AGENT_BOT_ID for x in mentions) or any(k in body_low for k in ("@物流小助手", "@hermes logistics-track", "@物流追踪机器人"))
        failed = [r for r in msg_items if r and not r.get("ok")]
        if failed:
            state = STORE.fail_message(channel_id, message_id,
                                       json.dumps(failed, ensure_ascii=False))
            print("message failed:", message_id, state, flush=True)
            continue
        STORE.complete_message(channel_id, message_id)
        if mentioned and not [r for r in msg_items if r]:
            cli(["im", "+agent-notify", "--target", "im", "--agent-slug", "logistics-track",
                 "--agent-name", "物流小助手", "--bot-name", "物流小助手",
                 "--channel-id", channel_id, "--no-json",
                 "--body", "【物流小助手】在的！把预报 xlsx、面单图片发到群里，或直接发文字配对（XSD…==1Z…），我就会自动查官网轨迹、通知录单人。"])
        actionable = [r for r in msg_items if r and r.get("ok") and
                      r.get("kind") in ("forecast", "label", "pair")]
        if actionable:
            STORE.enqueue_task("pipeline", f"pipeline:{message_id}",
                               {"channel_id": channel_id, "bot_app_id": bot_app_id})
            should_run = True
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
    if should_run and not os.environ.get("LOGI_NO_AUTOTRACK"):
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
            (DATA / ".watcher-heartbeat").touch()
            for r in results:
                if r: print(json.dumps(r, ensure_ascii=False), flush=True)
        except Exception as e:
            print("watcher error:", e, file=sys.stderr, flush=True)
        if args.once: break
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
