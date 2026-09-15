#!/usr/bin/env python
"""Delete old temporary files only after their inbox work is complete."""
import json
import os
import time
from pathlib import Path

from storage import Storage


def cleanup(store, directory, retention_days=30, now=None):
    cutoff = (now or time.time()) - retention_days * 86400
    protected = set()
    connection = store.connect()
    try:
        connection.execute("BEGIN")
        inbox = {row["id"]: json.loads(row["payload"]) for row in connection.execute(
            "SELECT id,payload FROM inbox")}
        protected_ids = {row["id"] for row in connection.execute(
            "SELECT id FROM inbox WHERE status IN ('pending','retry','running','dead')")}
        for row in connection.execute(
                "SELECT payload FROM tasks WHERE status IN "
                "('pending','retry','running','dead','unknown')"):
            payload = json.loads(row["payload"])
            source_id = payload.get("source_inbox_id") or payload.get("inbox_id")
            if source_id:
                protected_ids.add(str(source_id))
    finally:
        connection.close()
    for item_id in protected_ids:
        path = (inbox.get(item_id) or {}).get("path")
        if path:
            protected.add(str(Path(path).resolve()))
    deleted = []
    for path in Path(directory).glob("**/*"):
        if path.is_file() and path.stat().st_mtime < cutoff and str(path.resolve()) not in protected:
            path.unlink()
            deleted.append(str(path))
    return deleted


if __name__ == "__main__":
    cleanup(Storage(), os.environ.get("LOGIBOT_TMP_DIR") or "/app/tmp",
            int(os.environ.get("EVIDENCE_RETENTION_DAYS") or 30))
