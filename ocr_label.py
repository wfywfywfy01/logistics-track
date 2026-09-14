#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""面单 OCR: 调 Qwen 多模态接口提取 XSD==1Z 配对并 ingest-pair。
环境变量: OCR_BASE_URL / OCR_API_KEY / OCR_MODEL / OCR_CA_FILE(可选)
用法: python ocr_label.py --image <图片路径>
"""
import argparse, base64, json, os, re, signal, ssl, subprocess, sys, threading
from time import monotonic as time_monotonic
from pathlib import Path
from storage import Storage

ORDER_RE = re.compile(r"\b((?:XSD|CKD)[-\w]+)\b", re.I)
INTL_RE = re.compile(r"\b(1Z[A-Z0-9]{10,18}|[A-Z]{2}\d{8,14}|\d{9,14})\b", re.I)
PAIR_RE = re.compile(r"((?:XSD|CKD)[-\w]+)\s*(?:==|=|｜|\||\s)\s*(1Z[A-Z0-9]{10,18}|[A-Z]{2}\d{8,14}|\d{9,14})", re.I)


def _read_response(req, timeout, context):
    """Enforce a total HTTP deadline in the Linux production worker."""
    import urllib.request
    can_alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    previous = None
    if can_alarm:
        previous = signal.getsignal(signal.SIGALRM)

        def expired(_signum, _frame):
            raise TimeoutError("OCR request deadline exceeded")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=context).read()
    finally:
        if can_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


def image_as_data_url(path):
    b = open(path, "rb").read()
    mime = "image/png"
    if b[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        try:
            from PIL import Image
            import io
            im = Image.open(path).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            b = buf.getvalue()
            mime = "image/png"
        except Exception:
            pass
    return mime, base64.b64encode(b).decode()


def ocr_image(path):
    import urllib.request
    base = (os.environ.get("OCR_BASE_URL") or "https://qwen3.vertu.cn:8443")
    key = os.environ.get("OCR_API_KEY", "")
    model = (os.environ.get("OCR_MODEL") or "/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q8_0.gguf")
    ctx = ssl.create_default_context(cafile=os.environ.get("OCR_CA_FILE") or None)
    mime, img = image_as_data_url(path)
    prompt = ("Read the shipping label image. Output the order number (XSD... or CKD...) and the waybill/tracking number. One line per pair, format: order==tracking. If nothing, output NONE."
              "每行只输出一个配对，格式严格为: 订单号==国际单号。"
              "订单号形如 XSD260813138180；国际单号形如 1ZC23W53D442930184。"
              "没有就输出: 无。不要输出其他任何内容。")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + img}},
        ]}],
        "max_tokens": 500,
        "temperature": 0.1,
    }
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"authorization": "Bearer " + key,
                                          "content-type": "application/json"})
    timeout = min(60, max(1, int(os.environ.get("OCR_REQUEST_TIMEOUT") or 60)))
    d = json.loads(_read_response(req, timeout, ctx).decode("utf-8"))
    return d["choices"][0]["message"]["content"]


def extract_pairs(text, led=None):
    # 数字里的空格归一(OCR 常把 99 4130 5430 拆开)
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text or "")
    pairs, seen = [], set()
    for line in text.splitlines():
        for m in PAIR_RE.finditer(line):
            o, t = m.group(1).upper(), m.group(2).upper()
            if (o, t) not in seen:
                seen.add((o, t))
                pairs.append((o, t))
    if not pairs:
        os_ = list(dict.fromkeys(x.upper() for x in ORDER_RE.findall(text)))
        ts_ = list(dict.fromkeys(x.upper() for x in INTL_RE.findall(text)))
        if os_ and ts_:
            if len(os_) == 1:
                # 一个订单号配一个单号: 优先 1Z > 纯数字 > 字母+数字
                cand = [t for t in ts_ if t.startswith("1Z")] or [t for t in ts_ if t.isdigit()] or ts_
                pairs = [(os_[0], cand[0])]
            elif len(os_) == len(ts_):
                pairs = list(zip(os_, ts_))
    if led:
        by_intl = {v.get("intl"): k for k, v in led.items() if v.get("intl")}
        fixed = []
        for o, t in pairs:
            if o not in led and t in by_intl:
                fixed.append((by_intl[t], t))  # OCR 订单号读错时用单号反查
            elif o not in led:
                # OCR 漏读前缀(如漏掉 TH/DL/TW): 用数字尾段匹配台账订单
                tail = re.sub(r"^[A-Z]+-?", "", o)
                tail = re.sub(r"^[A-Z]+", "", tail)
                cand = [k for k in led if k.endswith(tail) and tail]
                if len(cand) == 1:
                    fixed.append((cand[0], t))
                else:
                    fixed.append((o, t))
            else:
                fixed.append((o, t))
        return fixed
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    a = ap.parse_args()
    budget = min(190, max(1, int(os.environ.get("OCR_PROCESS_BUDGET_SECONDS") or 190)))
    deadline = time_monotonic() + budget
    led = Storage().get_shipments()
    txt = ""
    pairs = []
    ocr_error = ""
    try:
        txt = ocr_image(a.image)
    except Exception as e:
        ocr_error = type(e).__name__ + ": " + str(e)[:300]
    pairs = extract_pairs(txt, led)
    ingested = []
    ingest_retryable = False
    for order, intl in pairs:
        remaining = int(deadline - time_monotonic())
        if remaining < 1:
            ingested.append({"order": order, "intl": intl, "ok": False,
                             "detail": "OCR process budget exhausted"})
            ingest_retryable = True
            break
        try:
            r = subprocess.run([sys.executable, "tracking-pipeline.py", "ingest-pair",
                                "--order", order, "--intl", intl],
                               capture_output=True, timeout=min(120, remaining))
            ingested.append({"order": order, "intl": intl, "ok": r.returncode == 0,
                             "detail": r.stdout.decode("utf-8", errors="replace")[:150]})
            if r.returncode not in (0, 2):
                ingest_retryable = True
                break
        except subprocess.TimeoutExpired:
            ingested.append({"order": order, "intl": intl, "ok": False,
                             "detail": "ingest-pair timed out"})
            ingest_retryable = True
            break
    retryable = bool((ocr_error and not pairs) or ingest_retryable)
    ok = bool(pairs) and not retryable and all(item["ok"] for item in ingested)
    error = ocr_error or ("ingest-pair failed or timed out" if ingest_retryable else
                          "OCR produced no pair" if not pairs else
                          "one or more OCR pairs could not be ingested")
    print(json.dumps({"ok": ok, "retryable": retryable, "error": error if not ok else "",
                      "text": txt, "pairs": pairs, "ingested": ingested}, ensure_ascii=False))
    return 0 if ok else (1 if retryable else 2)


if __name__ == "__main__":
    raise SystemExit(main())
