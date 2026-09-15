#!/usr/bin/env python
"""Verify and restore a logistics backup while the service is stopped."""
import argparse, hashlib, json, os, sqlite3, tempfile, zipfile
from pathlib import Path

def restore(archive_path, data_dir="data", force=False, tmp_dir=None):
    target = Path(data_dir) / "shipments.db"
    roots = {"data": Path(data_dir).resolve(),
             "tmp": Path(tmp_dir or os.environ.get("LOGIBOT_TMP_DIR") or "tmp").resolve()}
    if target.exists() and not force:
        raise FileExistsError("target exists; stop the service and pass --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temp_dir:
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            content = archive.read(manifest["database"])
            evidence = []
            for item in manifest.get("evidence") or []:
                root = item.get("root")
                relative = Path(str(item.get("path") or ""))
                if root not in roots or relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("invalid evidence path")
                evidence_content = archive.read(item["archive"])
                if hashlib.sha256(evidence_content).hexdigest() != item["sha256"]:
                    raise ValueError("evidence checksum mismatch")
                evidence.append((item, evidence_content))
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
    connection = sqlite3.connect(target)
    try:
        for item, evidence_content in evidence:
            evidence_target = roots[item["root"]] / item["path"]
            evidence_target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=evidence_target.parent, delete=False) as handle:
                handle.write(evidence_content)
                temporary = Path(handle.name)
            os.replace(temporary, evidence_target)
            row = connection.execute("SELECT payload FROM inbox WHERE id=?", (item["inbox_id"],)).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["path"] = str(evidence_target)
                connection.execute("UPDATE inbox SET payload=? WHERE id=?",
                                   (json.dumps(payload, ensure_ascii=False), item["inbox_id"]))
        connection.commit()
    finally:
        connection.close()
    return target

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--tmp-dir")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args()
    print(restore(options.archive, options.data_dir, options.force, options.tmp_dir))
