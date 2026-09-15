import os
import gc
import json
import time
import zipfile
from pathlib import Path
import pytest
from backup import create_backup
from restore_backup import restore
from storage import Storage

def test_verified_backup_round_trip(tmp_path):
    source = tmp_path / "source"
    Storage(source).upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报"})
    archive = create_backup(tmp_path / "backup.zip", source)
    target = tmp_path / "target"
    restore(archive, target)
    assert Storage(target).get_shipment("XSD1")["status"] == "已预报"

def test_restore_rejects_modified_backup(tmp_path):
    source = tmp_path / "source"
    Storage(source)
    archive = create_backup(tmp_path / "backup.zip", source)
    with zipfile.ZipFile(archive) as value:
        manifest = value.read("manifest.json")
    with zipfile.ZipFile(archive, "w") as value:
        value.writestr("manifest.json", manifest)
        value.writestr("shipments.db", b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        restore(archive, tmp_path / "target")


def test_backup_restores_registered_evidence_files(tmp_path):
    source = tmp_path / "source"
    source_tmp = tmp_path / "source-tmp"
    source_tmp.mkdir()
    label = source_tmp / "label.png"
    label.write_bytes(b"label-image")
    store = Storage(source)
    store.enqueue_inbox("label-1", {"order": "XSD1", "path": str(label)})

    archive = create_backup(tmp_path / "backup.zip", source, source_tmp)
    target = tmp_path / "target"
    target_tmp = tmp_path / "target-tmp"
    restore(archive, target, tmp_dir=target_tmp)

    assert (target_tmp / "label.png").read_bytes() == b"label-image"
    restored = Storage(target).get_inbox()[0]["payload"]
    assert restored["path"] == str(target_tmp / "label.png")


def test_restore_rejects_modified_evidence(tmp_path):
    source = tmp_path / "source"
    source_tmp = tmp_path / "source-tmp"
    source_tmp.mkdir()
    label = source_tmp / "label.png"
    label.write_bytes(b"label-image")
    store = Storage(source)
    store.enqueue_inbox("label-1", {"path": str(label)})
    archive = create_backup(tmp_path / "backup.zip", source, source_tmp)
    with zipfile.ZipFile(archive) as value:
        manifest = json.loads(value.read("manifest.json"))
        evidence_name = manifest["evidence"][0]["archive"]
    with zipfile.ZipFile(archive, "a") as value:
        value.writestr(evidence_name, b"tampered")

    with pytest.raises(ValueError, match="evidence checksum"):
        restore(archive, tmp_path / "target", tmp_dir=tmp_path / "target-tmp")


def test_backup_fails_when_required_evidence_is_missing(tmp_path):
    source = tmp_path / "source"
    store = Storage(source)
    store.enqueue_inbox("label-1", {"path": str(tmp_path / "missing.png")})

    with pytest.raises(FileNotFoundError, match="required evidence"):
        create_backup(tmp_path / "backup.zip", source, tmp_path / "source-tmp")


def test_backup_includes_succeeded_inbox_referenced_by_open_review(tmp_path):
    source = tmp_path / "source"
    source_tmp = tmp_path / "source-tmp"
    source_tmp.mkdir()
    label = source_tmp / "review.png"
    label.write_bytes(b"review-image")
    store = Storage(source)
    store.enqueue_inbox("label-review", {"path": str(label)})
    store.complete_inbox_with_task(
        "label-review", "review", "review:label-review",
        {"source_inbox_id": "label-review", "reason": "manual review"})

    archive = create_backup(tmp_path / "backup.zip", source, source_tmp)
    restore(archive, tmp_path / "target", tmp_dir=tmp_path / "target-tmp")

    assert (tmp_path / "target-tmp" / "review.png").read_bytes() == b"review-image"


def test_backup_evidence_selection_comes_from_database_snapshot(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source_tmp = tmp_path / "source-tmp"
    source_tmp.mkdir()
    label = source_tmp / "label.png"
    label.write_bytes(b"image")
    store = Storage(source)
    store.enqueue_inbox("label-1", {"path": str(label)})
    monkeypatch.setattr(Storage, "get_inbox",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("live read")))

    archive = create_backup(tmp_path / "backup.zip", source, source_tmp)

    assert archive.is_file()


def test_backup_hash_and_archive_use_same_evidence_bytes(monkeypatch, tmp_path):
    import hashlib
    source = tmp_path / "source"
    source_tmp = tmp_path / "source-tmp"
    source_tmp.mkdir()
    label = source_tmp / "label.png"
    label.write_bytes(b"original")
    store = Storage(source)
    store.enqueue_inbox("label-1", {"path": str(label)})
    real_read = Path.read_bytes

    def mutate_after_read(path):
        content = real_read(path)
        if path.resolve() == label.resolve():
            path.write_bytes(b"changed")
        return content

    monkeypatch.setattr(Path, "read_bytes", mutate_after_read)
    archive_path = create_backup(tmp_path / "backup.zip", source, source_tmp)

    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        item = manifest["evidence"][0]
        archived = archive.read(item["archive"])
    assert archived == b"original"
    assert hashlib.sha256(archived).hexdigest() == item["sha256"]


def test_restore_rolls_back_evidence_when_switch_fails(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source_tmp = tmp_path / "source-tmp"
    source_tmp.mkdir()
    store = Storage(source)
    store.upsert_shipment("NEW", {"orderNo": "NEW", "status": "已预报"})
    for item_id, name, content in (("one", "one.png", b"new-one"),
                                   ("two", "two.png", b"new-two")):
        path = source_tmp / name
        path.write_bytes(content)
        store.enqueue_inbox(item_id, {"path": str(path)})
    archive = create_backup(tmp_path / "backup.zip", source, source_tmp)

    target = tmp_path / "target"
    target_tmp = tmp_path / "target-tmp"
    target_tmp.mkdir()
    target_store = Storage(target)
    target_store.upsert_shipment("OLD", {"orderNo": "OLD", "status": "已预报"})
    del target_store
    gc.collect()
    (target_tmp / "one.png").write_bytes(b"old-one")
    real_replace = os.replace
    failed = False

    def fail_second_evidence(src, dst):
        nonlocal failed
        if not failed and Path(dst).name == "two.png":
            failed = True
            raise OSError("evidence switch failed")
        return real_replace(src, dst)

    monkeypatch.setattr("restore_backup.os.replace", fail_second_evidence)

    with pytest.raises(OSError, match="evidence switch failed"):
        restore(archive, target, force=True, tmp_dir=target_tmp)

    assert Storage(target).get_shipment("OLD") is not None
    assert Storage(target).get_shipment("NEW") is None
    assert (target_tmp / "one.png").read_bytes() == b"old-one"
    assert not (target_tmp / "two.png").exists()


def test_forced_restore_preserves_previous_database(tmp_path):
    source = tmp_path / "source"
    Storage(source).upsert_shipment("NEW", {"orderNo": "NEW", "status": "已预报"})
    archive = create_backup(tmp_path / "backup.zip", source)
    target = tmp_path / "target"
    target_store = Storage(target)
    target_store.upsert_shipment("OLD", {"orderNo": "OLD", "status": "已预报"})
    del target_store
    gc.collect()

    restore(archive, target, force=True)

    previous = Storage(target / "unused")
    previous.path = target / "shipments.db.pre-restore"
    assert previous.get_shipment("OLD") is not None


def test_backup_prunes_only_expired_managed_archives(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    expired = backup_dir / "logistics-backup-expired.zip"
    unrelated = backup_dir / "manual.zip"
    expired.write_bytes(b"old")
    unrelated.write_bytes(b"keep")
    old = time.time() - 2 * 86400
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")

    current = create_backup(backup_dir / "logistics-backup-current.zip", tmp_path / "data")

    assert current.exists()
    assert not expired.exists()
    assert unrelated.exists()


def test_invalid_retention_does_not_create_backup(monkeypatch, tmp_path):
    output = tmp_path / "logistics-backup-invalid.zip"
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "invalid")

    with pytest.raises(ValueError):
        create_backup(output, tmp_path / "data")

    assert not output.exists()
