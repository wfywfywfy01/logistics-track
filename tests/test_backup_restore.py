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
