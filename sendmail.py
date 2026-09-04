#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发邮件: python sendmail.py --to x@y --subject S --body B
SMTP 配置走环境变量 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS (腾讯企业邮箱 smtp.exmail.qq.com:465)
未配置 SMTP_USER/SMTP_PASS 时静默跳过(返回 rc=0)。
"""
import argparse, os, smtplib, ssl, sys
from email.header import Header
from email.mime.text import MIMEText


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True)
    a = ap.parse_args()
    host = (os.environ.get("SMTP_HOST") or "smtp.exmail.qq.com")
    port = int((os.environ.get("SMTP_PORT") or "465"))
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    if not user or not pwd:
        print("smtp not configured, skip", flush=True)
        return
    msg = MIMEText(a.body, "plain", "utf-8")
    msg["Subject"] = Header(a.subject, "utf-8")
    msg["From"] = user
    msg["To"] = a.to
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as s:
        s.login(user, pwd)
        s.sendmail(user, [a.to], msg.as_string())
    print("email sent to", a.to, flush=True)


if __name__ == "__main__":
    main()
