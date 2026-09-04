#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""台账备份到 V盘 (OSS): data/*.json -> zip -> drive +upload"""
import os, tempfile, time, zipfile
from pathlib import Path
import robust

BACKUP_PARENT = os.environ.get("BACKUP_PARENT_ID", "")


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zpath = os.path.join(tempfile.gettempdir(), "logistics-backup-%s.zip" % stamp)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(Path("data").glob("*.json")):
            z.write(f, arcname=f.name)
    args = ["drive", "+upload", "--source", zpath]
    if BACKUP_PARENT:
        args += ["--parent-id", BACKUP_PARENT]
    rc, out, _ = robust.cli_run(args, timeout=300)
    print("backup rc=%d %s" % (rc, out[:150]))
    try:
        os.unlink(zpath)
    except Exception:
        pass


if __name__ == "__main__":
    main()
