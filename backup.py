#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Create a verified SQLite backup and optionally upload it to V Drive."""
import argparse, hashlib, json, os, sqlite3, tempfile, time, zipfile
from pathlib import Path
import robust
from storage import Storage, iso

BACKUP_PARENT = os.environ.get("BACKUP_PARENT_ID", "")


def create_backup(output=None, data_dir=None):
    store = Storage(data_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(os.environ.get("BACKUP_DIR") or tempfile.gettempdir())
    backup_dir.mkdir(parents=True, exist_ok=True)
    output = Path(output or (backup_dir / f"logistics-backup-{stamp}.zip"))
    with tempfile.TemporaryDirectory() as temp_dir:
        snapshot = Path(temp_dir) / "shipments.db"
        source = store.connect()
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        check = sqlite3.connect(snapshot)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        finally:
            check.close()
        manifest = {"created_at": iso(), "database": "shipments.db",
                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "shipments.db")
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return output

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--upload", action="store_true")
    options = parser.parse_args()
    zpath = create_backup(options.output)
    should_upload = options.upload or os.environ.get("BACKUP_UPLOAD") == "1"
    if options.no_upload or not should_upload:
        print(zpath)
        return 0
    args = ["drive", "+upload", "--source", str(zpath)]
    if BACKUP_PARENT:
        args += ["--parent-id", BACKUP_PARENT]
    rc, out, _ = robust.cli_run(args, timeout=300)
    if rc != 0 or not out.strip():
        print("backup upload failed: %s" % out[:150])
        return 1
    print("backup uploaded:", zpath.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
