import os
from datetime import UTC, datetime, timedelta

from cleanup_evidence import cleanup
from storage import Storage


def test_dead_letter_evidence_is_retained(tmp_path):
    data = tmp_path / "data"; files = tmp_path / "tmp"
    files.mkdir(); old = (datetime.now(UTC) - timedelta(days=31)).timestamp()
    protected = files / "dead.png"; removable = files / "done.png"
    protected.write_bytes(b"dead"); removable.write_bytes(b"done")
    os.utime(protected, (old, old)); os.utime(removable, (old, old))
    store = Storage(data)
    store.enqueue_inbox("dead", {"path": str(protected)})
    item = store.claim_inbox("worker"); store.fail_inbox(item["id"], "failed", max_attempts=1)

    cleanup(store, files, retention_days=30)

    assert protected.exists()
    assert not removable.exists()
