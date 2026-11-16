import pytest

from test_reliability import load_pipeline


def test_sales_row_keeps_all_authoritative_recipient_ids(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)

    record = pipeline._row_to_rec({
        "订单号": "XSD1", "销售人员": "刘佳鑫",
        "销售user_id": "12537、12542、12549、12537",
    })

    assert record["salesperson"] == "刘佳鑫"
    assert record["sales_recipient_ids"] == [12537, 12542, 12549]
    assert not record.get("salesperson_id"), "A participant list does not map the primary name to its UID."


@pytest.mark.parametrize("raw", [None, "", "-", "0、-1、name", True])
def test_invalid_sales_identity_never_becomes_a_recipient(monkeypatch, tmp_path, raw):
    pipeline = load_pipeline(monkeypatch, tmp_path)

    record = pipeline._row_to_rec({"销售人员": "录单人", "销售user_id": raw})

    assert record.get("sales_recipient_ids", []) == []


def test_legacy_sales_cache_is_refreshed_to_add_participants(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.put_document("sales_map", {"XSD1": {"salesperson": "刘佳鑫"}})
    queried = []

    def query(args):
        queried.append(args)
        return {"rows": [{"订单号": "XSD1", "销售人员": "刘佳鑫",
                          "销售user_id": "12537、12549"}]}

    monkeypatch.setattr(pipeline, "cli_json", query)

    record = pipeline.match_sales("XSD1")

    assert len(queried) == 1
    assert record["sales_recipient_ids"] == [12537, 12549]
    assert pipeline.STORE.get_document("sales_map")["XSD1"]["sales_recipient_ids"] == [12537, 12549]


@pytest.mark.parametrize("operation", ["forecast", "pair", "rematch", "auto_create"])
def test_all_sales_matching_paths_store_participant_ids(monkeypatch, tmp_path, operation):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    if operation != "auto_create":
        pipeline.STORE.upsert_shipment("XSD1", {
            "orderNo": "XSD1", "status": "已预报", "history": [], "products": [],
            "salesperson": "刘佳鑫", "salesperson_id": 12549,
        })
    monkeypatch.setattr(pipeline, "match_sales", lambda *args, **kwargs: {
        "salesperson": "刘佳鑫", "sales_recipient_ids": [12537, 12549],
    })
    monkeypatch.setattr(pipeline, "parse_forecast", lambda path: [{"orderNo": "XSD1"}])

    if operation == "forecast":
        pipeline.ingest_forecast("forecast.xlsx")
    elif operation == "pair":
        pipeline.ingest_pair("XSD1", "876543210121")
    elif operation == "rematch":
        pipeline.rematch("XSD1")
    else:
        pipeline._create_order_from_sales("XSD1")

    assert pipeline.STORE.get_shipment("XSD1")["sales_recipient_ids"] == [12537, 12549]


def test_status_event_notifies_each_split_participant_once(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "history": [], "products": [],
        "intl": "876543210121", "salesperson": "刘佳鑫", "salesperson_id": 12549,
        "sales_recipient_ids": [12537, 12542, 12549, 12537],
    })

    pipeline.track_update("XSD1", "签收", observed_at="2026-11-16T00:00:00Z")
    pipeline._queue_notifications("channel")
    pipeline._queue_notifications("channel")

    tasks = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert sorted(task["payload"]["uid"] for task in tasks) == [12537, 12542, 12549]
    assert len({task["dedupe_key"] for task in tasks}) == 3
    assert all(str(task["payload"]["uid"]) in task["dedupe_key"] for task in tasks)


def test_split_uid_also_primary_or_family_recipient_is_not_duplicated(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.put_document("org_people", {"刘佳鑫": [12549], "何川": [12537]})
    for order, name, ids in (("XSD1", "刘佳鑫", [12537, 12549]),
                             ("XSD1-1", "何川", [12537, 12542])):
        pipeline.STORE.upsert_shipment(order, {
            "orderNo": order, "status": "运输中", "history": [], "products": [],
            "intl": "876543210121", "salesperson": name, "sales_recipient_ids": ids,
        })

    pipeline.track_update("XSD1", "异常", observed_at="2026-11-16T00:00:00Z")

    tasks = pipeline.STORE.list_tasks(kinds=("notify_dm",))
    assert sorted(task["payload"]["uid"] for task in tasks) == [12537, 12542, 12549]


def test_split_recipient_is_sent_by_uid_without_guessing_a_name(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    pipeline.STORE.upsert_shipment("XSD1", {
        "orderNo": "XSD1", "status": "运输中", "history": [], "products": [],
        "intl": "876543210121", "salesperson": "刘佳鑫", "salesperson_id": 12549,
        "sales_recipient_ids": [12537, 12549],
    })
    calls = []
    monkeypatch.setattr(pipeline, "cli", lambda args: calls.append(args) or "ok")
    monkeypatch.setattr(pipeline, "resolve_user", lambda name: pytest.fail("Known recipient UIDs need no name lookup."))
    pipeline.track_update("XSD1", "签收", observed_at="2026-11-16T00:00:00Z")

    pipeline.notify("channel", bot_app_id="test-bot")
    pipeline.notify("channel", bot_app_id="test-bot")

    user_ids = [args[args.index("--user-id") + 1] for args in calls if "--user-id" in args]
    assert sorted(user_ids) == ["12537", "12549"]


def test_partial_sales_page_cannot_establish_unique_participants(monkeypatch, tmp_path):
    pipeline = load_pipeline(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "cli_json", lambda args: {
        "rows": [{"订单号": "XSD1", "销售人员": "甲", "销售user_id": "12537"}],
        "pagination": {"has_more": True, "total_rows": 2},
    })
    assert not pipeline.match_sales("XSD1", force=True, cache=False).get("sales_recipient_ids")
