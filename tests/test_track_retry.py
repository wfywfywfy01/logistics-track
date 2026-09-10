from storage import Storage
from track_retry import retry_failed


def test_retry_targets_only_failed_package_and_preserves_versions(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "packages": [
        {"tracking": "A", "carrier": "UPS", "binding_version": 2},
        {"tracking": "B", "carrier": "FEDEX", "binding_version": 4}]})
    store.put_document("ups_results", {"XSD1": {"package_results": {
        "A": {"tracking": "A", "ok": True, "stage": "运输中"},
        "B": {"tracking": "B", "ok": False, "fails": 2}}}})
    calls = []
    result = retry_failed(store, lambda number, carrier: calls.append((number, carrier)) or
                          {"tracking": number, "carrier": carrier, "ok": True, "stage": "签收"})

    saved = store.get_document("ups_results", {})["XSD1"]
    assert calls == [("B", "FEDEX")]
    assert result == {"attempted": 1, "failed": 0}
    assert saved["package_results"]["B"]["binding_version"] == 4
    assert saved["stage"] == "部分签收"


def test_retry_merge_preserves_concurrent_order_result(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "intl": "A", "carrier": "UPS"})
    store.put_document("ups_results", {"XSD1": {"tracking": "A", "ok": False}})

    def tracker(number, _carrier):
        store.mutate_document("ups_results", lambda current: {
            **current, "XSD2": {"tracking": "B", "ok": True, "stage": "运输中"}}, {})
        return {"tracking": number, "ok": True, "stage": "运输中"}

    retry_failed(store, tracker)

    assert store.get_document("ups_results")["XSD2"]["tracking"] == "B"
