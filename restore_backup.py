#!/usr/bin/env python
"""Verify and restore a logistics backup while the service is stopped."""
import argparse, hashlib, json, os, sqlite3, tempfile, zipfile
from pathlib import Path

def restore(archive_path, data_dir="data", force=False):
    target = Path(data_dir) / "shipments.db"
    if target.exists() and not force:
        raise FileExistsError("target exists; stop the service and pass --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp_dir:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            content = archive.read(manifest["database"])
        if hashlib.sha256(content).hexdigest() != manifest["sha256"]:
            raise ValueError("backup checksum mismatch")
        restored = Path(temp_dir) / "shipments.db"
        restored.write_bytes(content)
        connection = sqlite3.connect(restored)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("restored database integrity check failed")
        finally:
            connection.close()
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(restored, target)
    return target

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args()
    print(restore(options.archive, options.data_dir, options.force))
