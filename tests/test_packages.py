from test_reliability import load_pipeline
import sqlite3


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
    replaced = pipeline.replace_package(
        "XSD1", "876543210121", "876543210124", "ops", "carrier relabelled")

    stale = pipeline.package_update("XSD1", "876543210121", "签收",
                                    binding_version=first["binding_version"])
    saved = pipeline.STORE.get_shipment("XSD1")
    assert stale["reason"] == "stale binding"
    assert replaced["binding_history"][0]["from"] == "876543210121"
    assert saved["packages"][0]["tracking"] == "876543210124"


def test_signed_package_replacement_archives_old_tracking_and_restarts_order(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "签收",
        "intl": "876543210121", "carrier": "FEDEX", "needs_notify": False,
        "history": [], "products": [], "packages": [{
            "tracking": "876543210121", "carrier": "FEDEX", "status": "签收",
            "binding_version": 2, "history": [{"to": "签收"}],
            "official_tracking": {"source": "fedex.com", "events": [{"status": "Delivered"}]},
            "last_observation": {"status": "签收", "observed_at": "2026-09-10T01:00:00Z"},
            "observation_observed_at": "2026-09-10T01:00:00Z",
            "status_observed_at": "2026-09-10T01:00:00Z", "binding_history": []}]})

    result = pipeline.replace_package(
        "XSD1", "876543210121", "876543210124", "ops", "carrier relabelled")

    saved = pipeline.STORE.get_shipment("XSD1")
    package = saved["packages"][0]
    assert result["replaced"] is True
    assert saved["status"] == "已出国际单" and saved["needs_notify"] is True
    assert package["tracking"] == "876543210124" and package["status"] == "已出国际单"
    assert "official_tracking" not in package and "last_observation" not in package
    assert package["history"] == []
    assert package["binding_history"][-1]["snapshot"]["official_tracking"]["events"][0][
        "status"] == "Delivered"
    assert pipeline.STORE.list_audit("XSD1")[0]["reason"] == "carrier relabelled"


def test_package_replacement_rejects_tracking_already_on_order(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "876543210121", "FedEx")
    pipeline.add_package("XSD1", "876543210122", "FedEx")

    result = pipeline.replace_package(
        "XSD1", "876543210121", "876543210122", "ops", "carrier relabelled")

    assert result == {"replaced": False, "error": "duplicate tracking"}


def test_package_change_and_audit_are_atomic(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "876543210121", "FedEx")
    with pipeline.STORE.connect() as connection:
        connection.execute(
            """CREATE TRIGGER reject_package_audit BEFORE INSERT ON audit_log
               WHEN NEW.entity_type='package' BEGIN SELECT RAISE(ABORT,'audit unavailable'); END""")

    try:
        pipeline.replace_package(
            "XSD1", "876543210121", "876543210124", "ops", "carrier relabelled")
        assert False, "audit failure must abort package replacement"
    except sqlite3.IntegrityError:
        pass
    assert pipeline.STORE.get_shipment("XSD1")["packages"][0]["tracking"] == "876543210121"


def test_package_replacement_requires_operator_and_reason(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "运输中",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "876543210121", "FedEx")

    assert pipeline.replace_package(
        "XSD1", "876543210121", "876543210124", "", "reason")["error"] == \
        "operator and reason are required"
    assert pipeline.replace_package(
        "XSD1", "876543210121", "876543210124", "ops", "")["error"] == \
        "operator and reason are required"


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


def test_package_update_persists_normalized_official_tracking(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "876543210123", "FedEx")
    official = {"source": "fedex.com", "estimated_delivery": None,
                "events": [{"occurred_at": "2026-09-10T01:00:00Z",
                            "status": "On the way"}]}

    pipeline.package_update("XSD1", "876543210123", "运输中",
                            observed_at="2026-09-10T01:00:00Z",
                            official_tracking=official)

    package = pipeline.STORE.get_shipment("XSD1")["packages"][0]
    assert package["official_tracking"] == official

    pipeline.package_update("XSD1", "876543210123", "运输中",
                            observed_at="2026-09-10T02:00:00Z")
    package = pipeline.STORE.get_shipment("XSD1")["packages"][0]
    assert package["official_tracking"] == official


def test_official_tracking_snapshot_loads_from_results_with_provenance(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    package_result = {"tracking": "1Z1", "ok": True, "source": "ups.com",
                      "observed_at": "2026-09-15T01:00:00Z", "status_en": "On the Way",
                      "events": [], "progress_steps": []}
    pipeline.STORE.put_document("ups_results", {"XSD1": {"package_results": {
        "1Z1": package_result}}})
    from official_tracking import result_hash

    snapshot = pipeline.official_tracking_from_results(
        "XSD1", "1Z1", expected_result_hash=result_hash(package_result))

    assert snapshot["source"] == "ups.com"
    assert snapshot["observed_at"] == "2026-09-15T01:00:00+00:00"
    assert snapshot["events"] == []


def test_same_stage_stale_observation_cannot_replace_new_official_snapshot(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "1Z1", "UPS")
    for observed, status in (("2026-09-15T01:00:00Z", "old"),
                             ("2026-09-15T03:00:00Z", "new")):
        pipeline.package_update("XSD1", "1Z1", "运输中", observed_at=observed,
                                official_tracking={"observed_at": observed, "status_en": status})

    result = pipeline.package_update(
        "XSD1", "1Z1", "运输中", observed_at="2026-09-15T02:00:00Z",
        official_tracking={"observed_at": "2026-09-15T02:00:00Z", "status_en": "stale"})

    package = pipeline.STORE.get_shipment("XSD1")["packages"][0]
    assert result["reason"] == "stale observation"
    assert package["official_tracking"]["status_en"] == "new"
    assert package["observation_observed_at"] == "2026-09-15T03:00:00+00:00"


def test_official_snapshot_rejects_missing_or_changed_observation_time(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.put_document("ups_results", {"XSD1": {"package_results": {
        "1Z1": {"tracking": "1Z1", "ok": True, "source": "ups.com",
                "events": [], "progress_steps": []}}}})
    import pytest
    from official_tracking import result_hash
    package_result = pipeline.STORE.get_document(
        "ups_results", {})["XSD1"]["package_results"]["1Z1"]
    with pytest.raises(ValueError, match="observed_at is required"):
        pipeline.official_tracking_from_results(
            "XSD1", "1Z1", expected_result_hash=result_hash(package_result))

    result = pipeline.STORE.get_document("ups_results", {})
    result["XSD1"]["package_results"]["1Z1"]["observed_at"] = "2026-09-15T03:00:00Z"
    pipeline.STORE.put_document("ups_results", result)
    with pytest.raises(ValueError, match="changed during apply"):
        pipeline.official_tracking_from_results(
            "XSD1", "1Z1", expected_observed_at="2026-09-15T02:00:00Z",
            expected_result_hash=result_hash(
                result["XSD1"]["package_results"]["1Z1"]))

    old_hash = result_hash(result["XSD1"]["package_results"]["1Z1"])
    result["XSD1"]["package_results"]["1Z1"]["status_en"] = "Exception"
    pipeline.STORE.put_document("ups_results", result)
    with pytest.raises(ValueError, match="changed during apply"):
        pipeline.official_tracking_from_results(
            "XSD1", "1Z1", expected_observed_at="2026-09-15T03:00:00Z",
            expected_result_hash=old_hash)


def test_same_watermark_only_allows_idempotent_official_snapshot(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "1Z1", "UPS")
    observed = "2026-09-15T01:00:00Z"
    pipeline.package_update("XSD1", "1Z1", "运输中", observed_at=observed,
                            official_tracking={"status_en": "On the Way"},
                            official_result_hash="a" * 64)

    result = pipeline.package_update(
        "XSD1", "1Z1", "运输中", observed_at=observed,
        official_tracking={"status_en": "Exception"}, official_result_hash="b" * 64)

    assert result["reason"] == "conflicting observation"
    assert pipeline.STORE.get_shipment("XSD1")["packages"][0][
        "official_tracking"]["status_en"] == "On the Way"


def test_api_snapshot_can_follow_dom_observation_at_same_new_watermark(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {"orderNo": "XSD1", "status": "已预报",
        "history": [], "products": []})
    pipeline.add_package("XSD1", "1Z1", "UPS")
    pipeline.package_update(
        "XSD1", "1Z1", "运输中", observed_at="2026-09-15T01:00:00Z",
        official_tracking={"observed_at": "2026-09-15T01:00:00Z", "status_en": "old"},
        official_result_hash="a" * 64)
    pipeline.package_update(
        "XSD1", "1Z1", "运输中", observed_at="2026-09-15T02:00:00Z")

    result = pipeline.package_update(
        "XSD1", "1Z1", "运输中", observed_at="2026-09-15T02:00:00Z",
        official_tracking={"observed_at": "2026-09-15T02:00:00Z", "status_en": "new"},
        official_result_hash="b" * 64)

    assert result.get("reason") != "conflicting observation"
    assert pipeline.STORE.get_shipment("XSD1")["packages"][0][
        "official_tracking"]["status_en"] == "new"


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
