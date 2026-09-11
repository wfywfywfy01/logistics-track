from test_reliability import load_pipeline


def test_three_packages_roll_up_to_partial_then_full_delivery(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})

    for number in ("876543210121", "876543210122", "876543210123"):
        result = pipeline.add_package("XSD1", number, "FedEx")
        assert result["added"] is True
    for number in ("876543210121", "876543210122"):
        pipeline.package_update("XSD1", number, "运输中", observed_at="2026-09-10T01:00:00Z")
    pipeline.package_update("XSD1", "876543210123", "签收",
                            observed_at="2026-09-10T02:00:00Z")
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "部分签收"

    for number in ("876543210121", "876543210122"):
        pipeline.package_update("XSD1", number, "签收", observed_at="2026-09-10T03:00:00Z")
    saved = pipeline.STORE.get_shipment("XSD1")
    assert saved["status"] == "签收"
    assert len(saved["packages"]) == 3


def test_package_replacement_keeps_history_and_rejects_stale_result(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})
    first = pipeline.add_package("XSD1", "876543210121", "FedEx")
    replaced = pipeline.replace_package("XSD1", "876543210121", "876543210124", "ops")

    stale = pipeline.package_update("XSD1", "876543210121", "签收",
                                    binding_version=first["binding_version"])
    saved = pipeline.STORE.get_shipment("XSD1")
    assert stale["reason"] == "stale binding"
    assert replaced["binding_history"][0]["from"] == "876543210121"
    assert saved["packages"][0]["tracking"] == "876543210124"


def test_package_status_does_not_move_backwards(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "876543210123", "FedEx")
    pipeline.package_update("XSD1", "876543210123", "运输中",
                            observed_at="2026-09-10T01:00:00Z")

    result = pipeline.package_update("XSD1", "876543210123", "已出国际单",
                                     observed_at="2026-09-10T02:00:00Z")

    assert result["changed"] is False
    assert pipeline.STORE.get_shipment("XSD1")["packages"][0]["status"] == "运输中"


def test_legacy_unversioned_package_accepts_version_zero(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "intl": "1Z1234567890", "history": [], "products": []})

    result = pipeline.package_update("XSD1", "1Z1234567890", "运输中",
                                     observed_at="2026-09-10T01:00:00+00:00",
                                     binding_version=0)

    assert result["changed"] is True
    assert result["status"] == "运输中"


def test_mismatched_binding_version_is_still_rejected(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "intl": "1Z1234567890", "binding_version": 2, "history": [], "products": []})

    result = pipeline.package_update("XSD1", "1Z1234567890", "运输中",
                                     observed_at="2026-09-10T01:00:00+00:00",
                                     binding_version=1)

    assert result["changed"] is False
    assert result["reason"] == "stale binding"


def test_legacy_alternate_package_detects_its_own_carrier(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    shipment = {"orderNo": "XSD1", "intl": "1Z999AA10123456784",
                "alt_intl": "876543210123", "carrier": "UPS", "status": "运输中"}

    packages = pipeline._packages(shipment)

    assert [item["carrier"] for item in packages] == ["UPS", "FEDEX"]
