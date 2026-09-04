#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""台账备份到 V盘 (OSS): data/*.json -> zip -> drive +upload"""
import os, subprocess, sys, time, zipfile
from pathlib import Path

BACKUP_PARENT = os.environ.get("BACKUP_PARENT_ID", "")


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zpath = "/tmp/logistics-backup-%s.zip" % stamp
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path("data").glob("*.json")):
            z.write(f, arcname=f.name)
    cmd = 'vertu-cli drive +upload --source "%s"' % zpath
    if BACKUP_PARENT:
        cmd += ' --parent-id "%s"' % BACKUP_PARENT
    r = subprocess.run(cmd, shell=True, capture_output=True)
    print("backup rc=%d %s" % (r.returncode, r.stdout.decode("utf-8", errors="replace")[:150]))
    try:
        os.unlink(zpath)
    except Exception:
        pass


if __name__ == "__main__":
    main()
