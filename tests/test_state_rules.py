from datetime import UTC, datetime

from test_reliability import load_pipeline


def shipment(status="已预报", **extra):
    return {"orderNo": "XSD1", "status": status, "history": [], "products": [], **extra}


def test_unknown_order_pair_enters_review_queue(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "match_sales", lambda *args, **kwargs: {})

    result = pipeline.ingest_pair("XSD-MISSING", "1Z1234567890")

    assert result["needs_review"] is True
    assert pipeline.STORE.get_shipment("XSD-MISSING") is None
    assert pipeline.STORE.pending_task_count("review") == 1


def test_conflicting_tracking_number_requires_review(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment(
        "XSD1", shipment(intl="1Z1111111111", binding_version=1)
    )

    result = pipeline.ingest_pair("XSD1", "1Z2222222222")

    assert result["needs_review"] is True
    assert pipeline.STORE.get_shipment("XSD1")["intl"] == "1Z1111111111"


def test_terminal_status_does_not_regress(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment(
        "XSD1", shipment(status="退回", intl="1Z1234567890", binding_version=1)
    )

    result = pipeline.track_update(
        "XSD1", "运输中", "old carrier observation",
        observed_at="2026-09-08T01:00:00+00:00", tracking="1Z1234567890",
        binding_version=1,
    )

    assert result == {"order": "XSD1", "status": "退回", "changed": False,
                      "reason": "terminal status"}


def test_result_from_old_tracking_binding_is_rejected(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment(
        "XSD1", shipment(intl="1Z2222222222", binding_version=2)
    )

    result = pipeline.track_update(
        "XSD1", "运输中", "stale result",
        observed_at=datetime(2026, 9, 8, tzinfo=UTC).isoformat(),
        tracking="1Z1111111111", binding_version=1,
    )

    assert result["changed"] is False
    assert result["reason"] == "stale binding"
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "已预报"


def test_unknown_carrier_status_is_not_stored_as_fact(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", shipment())

    result = pipeline.track_update("XSD1", "carrier invented state")

    assert result["changed"] is False
    assert result["reason"] == "unknown status"
    assert pipeline.STORE.get_shipment("XSD1")["status"] == "已预报"


def test_sales_lookup_requires_one_exact_order(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "cli_json", lambda _args: {"rows": [
        {"订单号": "XSD-OTHER", "销售人员": "错误的人"},
        {"订单号": "XSD-100", "销售人员": "正确的人"},
    ]})
    assert pipeline.match_sales("XSD-100")["salesperson"] == "正确的人"


def test_ambiguous_employee_name_does_not_resolve(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.put_document("org_people", {"同名员工": ["u1", "u2"]})
    monkeypatch.setattr(pipeline, "cli_json", lambda _args: None)
    assert pipeline.resolve_user("同名员工") is None
