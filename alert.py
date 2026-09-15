#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Compatibility entry point; all alerts use the durable notification queue."""
import os
import subprocess
import sys


def main():
    channel_id = os.environ.get("CHANNEL_ID")
    if not channel_id:
        print("CHANNEL_ID is required", file=sys.stderr)
        return 2
    args = [sys.executable, "tracking-pipeline.py", "notify", "--channel-id", channel_id]
    bot_app_id = os.environ.get("BOT_APP_ID")
    if bot_app_id:
        args.extend(["--bot-app-id", bot_app_id])
    return subprocess.run(args).returncode


if __name__ == "__main__":
    raise SystemExit(main())
