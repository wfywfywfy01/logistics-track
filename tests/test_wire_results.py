from storage import Storage
from wire_results import apply_results


def test_package_results_are_applied_individually(tmp_path):
    store = Storage(tmp_path)
    store.upsert_shipment("XSD1", {"orderNo": "XSD1", "intl": "A"})
    store.put_document("ups_results", {"XSD1": {"package_results": {
        "A": {"tracking": "A", "ok": True, "stage": "运输中",
              "observed_at": "2026-09-10T00:00:00Z", "binding_version": 1},
        "B": {"tracking": "B", "ok": False, "error": "timeout"},
    }}})
    calls = []

    result = apply_results(store, lambda args: (calls.append(args) or (0, "ok")))

    assert result == {"applied": 1, "failed": 0}
    assert calls[0][:5] == ["package-update", "--order", "XSD1", "--tracking", "A"]
