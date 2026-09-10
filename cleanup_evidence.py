#!/usr/bin/env python
"""Delete old temporary files only after their inbox work is complete."""
import os
import time
from pathlib import Path

from storage import Storage


def cleanup(store, directory, retention_days=30, now=None):
    cutoff = (now or time.time()) - retention_days * 86400
    protected = set()
    for item in store.get_inbox(("pending", "retry", "running", "dead")):
        path = (item.get("payload") or {}).get("path")
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
