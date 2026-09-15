#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Create a verified SQLite backup and optionally upload it to V Drive."""
import argparse, hashlib, json, os, sqlite3, tempfile, time, zipfile
from pathlib import Path
import robust
from storage import Storage, iso

BACKUP_PARENT = os.environ.get("BACKUP_PARENT_ID", "")


def prune_backups(backup_dir, retention_days, keep=None, now=None):
    retention_days = int(retention_days)
    if retention_days <= 0:
        raise ValueError("BACKUP_RETENTION_DAYS must be positive")
    cutoff = (now or time.time()) - retention_days * 86400
    keep = Path(keep).resolve() if keep else None
    for archive in Path(backup_dir).glob("logistics-backup-*.zip"):
        if archive.resolve() != keep and archive.stat().st_mtime < cutoff:
            archive.unlink()


def create_backup(output=None, data_dir=None, tmp_dir=None):
    retention_days = int(os.environ.get("BACKUP_RETENTION_DAYS") or 30)
    if retention_days <= 0:
        raise ValueError("BACKUP_RETENTION_DAYS must be positive")
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
        check.row_factory = sqlite3.Row
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
            inbox_rows = {row["id"]: dict(row) for row in check.execute(
                "SELECT id,payload,status FROM inbox")}
            protected_ids = {item_id for item_id, row in inbox_rows.items()
                             if row["status"] in ("pending", "retry", "running", "dead")}
            for row in check.execute(
                    "SELECT payload FROM tasks WHERE status IN "
                    "('pending','retry','running','dead','unknown')"):
                payload = json.loads(row["payload"])
                source_id = payload.get("source_inbox_id") or payload.get("inbox_id")
                if source_id:
                    protected_ids.add(str(source_id))
        finally:
            check.close()
        tmp_dir = Path(tmp_dir or os.environ.get("LOGIBOT_TMP_DIR") or
                       ("/app/tmp" if Path("/app/tmp").is_dir() else "tmp")).resolve()
        roots = {"data": store.data_dir.resolve(), "tmp": tmp_dir}
        evidence = []
        staged_evidence = {}
        for item_id in sorted(protected_ids):
            item = inbox_rows.get(item_id)
            if not item:
                continue
            payload = json.loads(item["payload"])
            raw = str(payload.get("path") or "")
            if not raw:
                continue
            path = Path(raw)
            candidates = [path.resolve()] if path.is_absolute() else [
                (Path.cwd() / path).resolve(), (tmp_dir / path.name).resolve(),
                (store.data_dir / path.name).resolve()]
            match = next(((name, candidate.relative_to(root))
                          for candidate in candidates if candidate.is_file()
                          for name, root in roots.items()
                          if candidate == root or candidate.is_relative_to(root)), None)
            if not match:
                raise FileNotFoundError("required evidence is missing: inbox %s" % item_id)
            root_name, relative = match
            archive_name = "evidence/%s/%s/%s" % (
                root_name, hashlib.sha256(item_id.encode()).hexdigest()[:16],
                relative.as_posix())
            content = (roots[root_name] / relative).read_bytes()
            evidence.append({"inbox_id": item_id, "root": root_name,
                             "path": relative.as_posix(), "archive": archive_name,
                             "sha256": hashlib.sha256(content).hexdigest()})
            staged = Path(temp_dir) / archive_name
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
            staged_evidence[archive_name] = staged
        manifest = {"created_at": iso(), "database": "shipments.db",
                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                    "evidence": evidence}
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, "shipments.db")
            for item in evidence:
                archive.write(staged_evidence[item["archive"]], item["archive"])
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    prune_backups(backup_dir, retention_days, keep=output)
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
