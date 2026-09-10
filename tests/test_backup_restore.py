import os
import time
import zipfile
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
