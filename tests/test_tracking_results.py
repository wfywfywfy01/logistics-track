import importlib.util
import sys
import types

def load_module(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["track_all_ups.py"])
    monkeypatch.setitem(sys.modules, "ups_track", types.SimpleNamespace(track_ups=lambda _tn: {}))
    monkeypatch.setitem(sys.modules, "dhl_track", types.SimpleNamespace(track_dhl=lambda _tn: {}))
    monkeypatch.setitem(sys.modules, "fedex_track", types.SimpleNamespace(track_fedex=lambda _tn: {}))
    path = __import__("pathlib").Path(__file__).parents[1] / "track_all_ups.py"
    spec = importlib.util.spec_from_file_location("track_all_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_primary_and_alternate_completion_order_is_stable(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    primary = {"tracking": "1ZPRIMARY", "ok": False, "error": "down"}
    alternate = {"tracking": "ALT", "ok": True, "stage": "运输中"}
    first_alt = module.merge_result(module.merge_result({}, alternate, True), primary, False)
    first_primary = module.merge_result(module.merge_result({}, primary, False), alternate, True)
    assert first_alt["tracking"] == first_primary["tracking"] == "ALT"
    assert first_alt["stage"] == first_primary["stage"] == "运输中"


def test_multiple_packages_roll_up_independent_of_completion_order(monkeypatch, tmp_path):
    module = load_module(monkeypatch, tmp_path)
    expected = ["A", "B", "C"]
    rows = [
        {"tracking": "A", "ok": True, "stage": "运输中"},
        {"tracking": "B", "ok": True, "stage": "运输中"},
        {"tracking": "C", "ok": True, "stage": "签收"},
    ]
    forward = {}
    reverse = {}
    for row in rows: forward = module.merge_package_result(forward, row, expected)
    for row in reversed(rows): reverse = module.merge_package_result(reverse, row, expected)
    assert forward["stage"] == reverse["stage"] == "部分签收"
    assert forward["ok"] is reverse["ok"] is True
