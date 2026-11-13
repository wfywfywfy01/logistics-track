import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from official_tracking import result_hash
from storage import Storage
from wire_results import apply_results


ROOT = Path(__file__).resolve().parents[1]
TRACKING = "1ZLOCALTEST00000001"
OBSERVED = "2026-11-13T08:00:00Z"


def official_result(**updates):
    return {"order": "ORDER1", "tracking": TRACKING, "carrier": "UPS",
            "binding_version": 1, "observed_at": OBSERVED, "ok": True,
            "stage": "运输中", "source": "ups.com", "status_en": "On the Way",
            "events": [{"status": "On the Way", "location": "Test fixture"}],
            "progress_steps": [], **updates}


@pytest.fixture
def store(tmp_path):
    store = Storage(tmp_path / "data")
    store.upsert_shipment("ORDER1", {"orderNo": "ORDER1", "intl": TRACKING,
        "status": "运输中", "packages": [{"tracking": TRACKING, "active": True,
            "carrier": "UPS", "binding_version": 1, "status": "运输中"}]})
    return store


def run_merge(store, tmp_path, rows, apply_now=False):
    source = tmp_path / "local-results.json"
    source.write_text(json.dumps(rows), encoding="utf-8")
    command = [sys.executable, str(ROOT / "tools/server_merge_local_results.py"), str(source)]
    if apply_now:
        command.append("--apply")
    completed = subprocess.run(command, cwd=ROOT if apply_now else tmp_path, capture_output=True, text=True,
        encoding="utf-8", timeout=20,
        env={**os.environ, "LOGIBOT_DATA_DIR": str(store.data_dir), "PYTHONPATH": str(ROOT),
             "PYTHONIOENCODING": "utf-8"})
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def test_single_package_flows_through_official_snapshot_validation(store, tmp_path):
    row = official_result()
    run_merge(store, tmp_path, {TRACKING: row})
    stored = store.get_document("ups_results", {})["ORDER1"]
    assert stored["package_results"][TRACKING] == row
    calls = []
    assert apply_results(store, lambda args: (calls.append(args) or (0, "ok"))) == {
        "applied": 1, "failed": 0}
    assert calls[0][0] == "package-update"
    assert "--official-from-results" in calls[0]
    assert calls[0][calls[0].index("--official-result-hash") + 1] == result_hash(row)
    assert calls[0][calls[0].index("--binding-version") + 1] == "1"
    assert calls[0][calls[0].index("--observed-at") + 1] == OBSERVED


def test_merging_one_package_preserves_sibling_and_unrelated_order(store, tmp_path):
    shipment = store.get_shipment("ORDER1")
    shipment["packages"].append({"tracking": "DHL-TEST", "carrier": "DHL", "binding_version": 2})
    store.upsert_shipment("ORDER1", shipment)
    sibling = official_result(tracking="DHL-TEST", carrier="DHL", source="dhl.com",
                              binding_version=2, stage="签收")
    other = {"tracking": "OTHER", "ok": False, "error": "existing failure"}
    store.put_document("ups_results", {"ORDER1": {"package_results": {"DHL-TEST": sibling}},
                                       "ORDER2": other})
    run_merge(store, tmp_path, {TRACKING: official_result()})
    document = store.get_document("ups_results", {})
    assert document["ORDER1"]["package_results"]["DHL-TEST"] == sibling
    assert document["ORDER1"]["stage"] == "部分签收"
    assert document["ORDER2"] == other


@pytest.mark.parametrize("change", ["wrong_version", "inactive", "wrong_tracking",
    "key_mismatch", "wrong_order", "missing_timestamp", "naive_timestamp", "bad_timestamp",
    "wrong_source", "missing_events"])
def test_invalid_or_unbound_rows_do_not_enter_result_cache(store, tmp_path, change):
    row = official_result()
    if change == "wrong_version":
        row["binding_version"] = 2
    elif change == "inactive":
        shipment = store.get_shipment("ORDER1")
        shipment["packages"][0]["active"] = False
        store.upsert_shipment("ORDER1", shipment)
    elif change == "wrong_tracking":
        row["tracking"] = "1ZOTHERTEST00000001"
    elif change == "wrong_order":
        row["order"] = "MISSING"
    elif change == "missing_timestamp":
        row.pop("observed_at")
    elif change == "naive_timestamp":
        row["observed_at"] = "2026-11-13T08:00:00"
    elif change == "bad_timestamp":
        row["observed_at"] = "not-a-date"
    elif change == "wrong_source":
        row["source"] = "unverified.example"
    elif change == "missing_events":
        row.pop("events")
    key = "WRONG-KEY" if change == "key_mismatch" else row["tracking"]
    run_merge(store, tmp_path, {key: row})
    assert store.get_document("ups_results", {}) == {}


@pytest.mark.parametrize("newer_location", ["cache", "shipment"])
def test_old_observation_cannot_replace_newer_cache_or_saved_snapshot(store, tmp_path, newer_location):
    newer = official_result(observed_at="2026-11-13T09:00:00+00:00")
    if newer_location == "cache":
        store.put_document("ups_results", {"ORDER1": {"package_results": {TRACKING: newer}}})
    else:
        shipment = store.get_shipment("ORDER1")
        shipment["packages"][0]["official_tracking"] = newer
        store.upsert_shipment("ORDER1", shipment)
    before = store.get_document("ups_results", {})
    run_merge(store, tmp_path, {TRACKING: official_result()})
    assert store.get_document("ups_results", {}) == before


@pytest.mark.parametrize("conflict_location", ["cache", "shipment"])
def test_same_instant_conflict_is_rejected(store, tmp_path, conflict_location):
    original = official_result()
    if conflict_location == "cache":
        store.put_document("ups_results", {"ORDER1": {"package_results": {TRACKING: original}}})
    else:
        shipment = store.get_shipment("ORDER1")
        shipment["packages"][0]["official_tracking"] = {
            "observed_at": OBSERVED, "result_hash": result_hash(original)}
        store.upsert_shipment("ORDER1", shipment)
    before = store.get_document("ups_results", {})
    run_merge(store, tmp_path, {TRACKING: official_result(stage="签收")})
    assert store.get_document("ups_results", {}) == before


def test_failure_cannot_replace_success_and_replay_is_idempotent(store, tmp_path):
    row = official_result()
    run_merge(store, tmp_path, {TRACKING: row})
    before = copy.deepcopy(store.get_document("ups_results", {}))
    replay = run_merge(store, tmp_path, {TRACKING: row})
    assert replay["merged"] == 0
    assert store.get_document("ups_results", {}) == before
    run_merge(store, tmp_path, {TRACKING: official_result(
        ok=False, error="no GetStatus data", observed_at="2026-11-13T10:00:00Z")})
    assert store.get_document("ups_results", {}) == before


def test_legacy_single_result_is_preserved_when_sibling_arrives(store, tmp_path):
    previous = official_result()
    next_tracking = "1ZLOCALTEST00000002"
    shipment = store.get_shipment("ORDER1")
    shipment["packages"].append({"tracking": next_tracking, "carrier": "UPS", "binding_version": 2})
    store.upsert_shipment("ORDER1", shipment)
    store.put_document("ups_results", {"ORDER1": previous})
    row = official_result(tracking=next_tracking, binding_version=2)
    run_merge(store, tmp_path, {next_tracking: row})
    assert store.get_document("ups_results", {})["ORDER1"]["package_results"] == {
        TRACKING: previous, next_tracking: row}


def test_missing_sibling_does_not_mark_order_fully_delivered(store, tmp_path):
    shipment = store.get_shipment("ORDER1")
    shipment["packages"].append({"tracking": "DHL-TEST", "carrier": "DHL", "binding_version": 2})
    store.upsert_shipment("ORDER1", shipment)
    run_merge(store, tmp_path, {TRACKING: official_result(stage="签收")})
    result = store.get_document("ups_results", {})["ORDER1"]
    assert not result["ok"]
    assert result["partial"]
    assert result["stage"] == "部分签收"


def test_bindings_are_read_while_result_write_transaction_is_held(store, monkeypatch):
    from tools.server_merge_local_results import merge_results
    read_shipments = store.get_shipments
    mutate_document = store.mutate_document

    def rebind_before_transaction(name, updater, default=None):
        shipment = store.get_shipment("ORDER1")
        shipment["packages"][0]["binding_version"] = 2
        store.upsert_shipment("ORDER1", shipment)
        return mutate_document(name, updater, default)

    def read_under_lock():
        with sqlite3.connect(store.path, timeout=0) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
        return read_shipments()

    monkeypatch.setattr(store, "mutate_document", rebind_before_transaction)
    monkeypatch.setattr(store, "get_shipments", read_under_lock)
    result = merge_results(store, {TRACKING: official_result()})
    assert result["merged"] == 0
    assert result["skipped"] == {"stale_binding": 1}
    assert store.get_document("ups_results", {}) == {}


def test_old_binding_cache_does_not_block_current_binding_result(store, tmp_path):
    shipment = store.get_shipment("ORDER1")
    shipment["packages"][0]["binding_version"] = 2
    store.upsert_shipment("ORDER1", shipment)
    store.put_document("ups_results", {"ORDER1": {"package_results": {TRACKING:
        official_result(binding_version=1, observed_at="2026-11-13T10:00:00Z")}}})
    row = official_result(binding_version=2)
    run_merge(store, tmp_path, {TRACKING: row})
    assert store.get_document("ups_results", {})["ORDER1"]["package_results"][TRACKING] == row


def test_future_local_clock_cannot_advance_server_watermark(store, tmp_path):
    row = official_result(observed_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    result = run_merge(store, tmp_path, {TRACKING: row})
    assert result["merged"] == 0
    assert result["skipped"] == {"future_observation": 1}
    assert store.get_document("ups_results", {}) == {}


def test_apply_option_holds_batch_lock_and_scopes_orders(store, tmp_path, monkeypatch, capsys):
    import robust
    from tools import server_merge_local_results as merge
    source = tmp_path / "apply-results.json"
    source.write_text(json.dumps({TRACKING: official_result()}), encoding="utf-8")
    calls = []

    def apply_under_lock(actual_store, *, orders):
        assert actual_store is store
        competing = robust.FileLock(store.data_dir / ".auto-track.lock", timeout=0)
        assert competing.acquire() is False
        calls.append(orders)
        return {"applied": 1, "failed": 0}

    monkeypatch.setattr(merge, "Storage", lambda: store)
    monkeypatch.setattr(merge, "apply_results", apply_under_lock)
    assert merge.main([str(source), "--apply"]) == 0
    assert calls == [{"ORDER1"}]
    assert json.loads(capsys.readouterr().out)["apply"] == {"applied": 1, "failed": 0}


def test_optional_wire_order_scope_does_not_process_other_cached_results(store):
    store.upsert_shipment("ORDER2", {"intl": "OTHER"})
    store.put_document("ups_results", {
        "ORDER1": {"package_results": {TRACKING: official_result()}},
        "ORDER2": {"ok": True, "tracking": "OTHER", "stage": "运输中"}})
    calls = []
    outcome = apply_results(store, lambda args: (calls.append(args) or (0, "ok")), orders={"ORDER1"})
    assert outcome == {"applied": 1, "failed": 0}
    assert len(calls) == 1 and calls[0][calls[0].index("--order") + 1] == "ORDER1"
    assert apply_results(store, lambda args: pytest.fail("empty scope must not apply anything"),
                         orders=set()) == {"applied": 0, "failed": 0}


def test_cli_apply_persists_official_snapshot_and_replay_adds_no_event(store, tmp_path):
    row = official_result(stage="签收")
    outcome = run_merge(store, tmp_path, {TRACKING: row}, apply_now=True)
    assert outcome["apply"] == {"applied": 1, "failed": 0}
    saved = store.get_shipment("ORDER1")
    assert saved["packages"][0]["official_tracking"]["events"] == row["events"]
    assert saved["packages"][0]["official_tracking"]["result_hash"] == result_hash(row)
    assert saved["status"] == "签收"
    history = saved["history"]
    tasks = store.list_tasks()
    run_merge(store, tmp_path, {TRACKING: row}, apply_now=True)
    assert store.get_shipment("ORDER1")["history"] == history
    assert store.list_tasks() == tasks
