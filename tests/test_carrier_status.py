from dhl_track import classify_status as classify_dhl, parse_tracking_response as parse_dhl_response
from fedex_track import classify_status as classify_fedex, parse_tracking_response
from carriers import detect_carrier
from ups_track import classify_status as classify_ups
import ups_track
import sys
import types


class _LateResponsePage:
    def __init__(self, response, body="", send_on_goto=False):
        self.response = response
        self.body = body
        self.send_on_goto = send_on_goto
        self.handler = None
        self.sent = False

    def on(self, event, handler):
        if event == "response":
            self.handler = handler

    def goto(self, *_args, **_kwargs):
        if self.send_on_goto and not self.sent:
            self.sent = True
            self.handler(self.response)
        return None

    def query_selector(self, _selector):
        return None

    def wait_for_timeout(self, _milliseconds):
        if not self.sent:
            self.sent = True
            self.handler(self.response)

    def locator(self, _selector):
        return self

    def inner_text(self):
        return self.body


def _install_fake_browser(monkeypatch, page):
    browser = types.SimpleNamespace(
        closed=False,
        new_context=lambda **_kwargs: types.SimpleNamespace(new_page=lambda: page),
    )
    browser.close = lambda: setattr(browser, "closed", True)
    page.response.browser = browser
    playwright = types.SimpleNamespace(
        chromium=types.SimpleNamespace(launch=lambda **_kwargs: browser))

    class Context:
        def __enter__(self):
            return playwright

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(sys.modules, "patchright.sync_api", types.SimpleNamespace(
        sync_playwright=lambda: Context()))
    return browser


class _LateResponse:
    def __init__(self, url, payload, status=200):
        self.url = url
        self.payload = payload
        self.status = status
        self.browser = None

    def json(self):
        assert not self.browser.closed
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_unknown_ups_status_is_not_guessed_as_in_transit():
    assert classify_ups("Z", "A newly introduced carrier state") is None


def test_unknown_dhl_status_is_not_guessed_as_in_transit():
    assert classify_dhl("brand_new", "A newly introduced carrier state") is None


def test_known_carrier_statuses_are_classified():
    assert classify_ups("D", "Delivered") == "签收"
    assert classify_dhl("transit", "Departed facility") == "运输中"
    assert classify_fedex("DL", "Delivered") == "签收"
    assert classify_fedex("DE", "Delivery exception") == "异常"
    assert classify_fedex("RP", "Return label link emailed") is None


def test_carrier_detection_prefers_explicit_carrier_and_fails_closed():
    assert detect_carrier("1Z999AA10123456784") == "UPS"
    assert detect_carrier("1234567890") == "DHL"
    assert detect_carrier("876543210123") == "FEDEX"
    assert detect_carrier("123456789012", "DHL国际") == "DHL"
    assert detect_carrier("ABC123") is None


def test_fedex_official_response_is_normalized():
    payload = {
        "output": {"completeTrackResults": [{"trackResults": [{
            "trackingNumberInfo": {"trackingNumber": "876543210123"},
            "latestStatusDetail": {
                "code": "IT", "statusByLocale": "On the way",
                "scanLocation": {"city": "MEMPHIS", "countryCode": "US"},
            },
            "estimatedDeliveryTimeWindow": {"window": {
                "begins": "2026-09-11T09:00:00-05:00",
                "ends": "2026-09-11T17:00:00-05:00"}},
            "scanEvents": [{
                "date": "2026-09-10T10:20:00-05:00", "eventDescription": "Departed facility",
                "scanLocation": {"city": "MEMPHIS", "countryCode": "US"},
            }],
        }]}]},
    }

    result = parse_tracking_response("876543210123", payload)

    assert result == {
        "tracking": "876543210123", "ok": True, "stage": "运输中",
        "status_en": "On the way",
        "detail": "2026-09-10T10:20:00-05:00 MEMPHIS US Departed facility",
        "source": "fedex.com",
        "estimated_delivery": {
            "local_from_text": "2026-09-11T09:00:00-05:00",
            "local_through_text": "2026-09-11T17:00:00-05:00",
            "from_utc": "2026-09-11T14:00:00Z",
            "through_utc": "2026-09-11T22:00:00Z",
        },
        "latest_event": {
            "source_time_text": "2026-09-10T10:20:00-05:00",
            "occurred_at_utc": "2026-09-10T15:20:00Z",
            "location": "MEMPHIS US", "status": "Departed facility",
            "description": "", "code": "", "exception_code": "",
        },
        "progress_steps": [], "progress_steps_availability": "N/A",
        "events": [{
            "source_time_text": "2026-09-10T10:20:00-05:00",
            "occurred_at_utc": "2026-09-10T15:20:00Z",
            "location": "MEMPHIS US", "status": "Departed facility",
            "description": "", "code": "", "exception_code": "",
        }],
    }


def test_dhl_official_response_includes_eta_and_complete_history():
    shipment = {
        "id": "1234567890", "service": "express",
        "status": {"timestamp": "2026-09-14T16:00:00+03:00",
                   "statusCode": "transit", "description": "Shipment is on hold",
                   "location": {"address": {"addressLocality": "Riga", "countryCode": "LV"}}},
        "estimatedDeliveryTimeFrame": {
            "estimatedFrom": "2026-09-15T09:00:00+03:00",
            "estimatedThrough": "2026-09-15T17:00:00+03:00"},
        "events": [
            {"timestamp": "2026-09-14T16:00:00+03:00", "statusCode": "transit",
             "status": "OH", "description": "Shipment is on hold",
             "remark": "Awaiting payment", "location": {"address": {
                 "addressLocality": "Riga", "countryCode": "LV"}}},
            {"timestamp": "2026-09-14T10:00:00+03:00", "statusCode": "transit",
             "status": "AR", "description": "Arrived at facility"},
        ],
    }

    result = parse_dhl_response("1234567890", shipment)

    assert result["source"] == "dhl.com"
    assert result["estimated_delivery"]["local_from_text"] == "2026-09-15T09:00:00+03:00"
    assert result["estimated_delivery"]["from_utc"] == "2026-09-15T06:00:00Z"
    assert result["latest_event"]["location"] == "Riga LV"
    assert result["latest_event"]["occurred_at_utc"] == "2026-09-14T13:00:00Z"
    assert result["events"][0]["description"] == "Awaiting payment"
    assert len(result["events"]) == 2
    assert result["progress_steps_availability"] == "N/A"


def test_ups_official_response_is_normalized():
    payload = {"trackDetails": [{
        "trackingNumber": "1Z999AA10123456784",
        "packageStatusType": "D",
        "packageStatus": "Delivered",
        "progressBarPercentage": "100",
        "receivedBy": "FRONT DESK",
        "milestones": [{"isCurrent": True, "date": "09/10/2026",
                        "time": "10:45", "location": "MEMPHIS, TN",
                        "name": "Delivered"}],
    }]}

    result = ups_track.parse_tracking_response("1Z999AA10123456784", payload)

    assert result["ok"] is True
    assert result["stage"] == "签收"
    assert result["received_by"] == "FRONT DESK"
    assert "MEMPHIS" in result["detail"]


def test_ups_official_response_includes_eta_progress_and_complete_history():
    payload = {"trackDetails": [{
        "trackingNumber": "1Z999AA10123456784", "packageStatusType": "X",
        "packageStatus": "Update", "progressBarPercentage": "86",
        "progressBarType": "Exception", "sdd": "20260915", "sdt": "140000",
        "shipmentGMTInfo": {"shipToGMTOffset": "+03:00"},
        "currentMilestone": {"date": "09/14/2026", "time": "4:00 P.M.",
                             "location": "Riga, Latvia", "name": "On the Way"},
        "milestones": [
            {"name": "Label Created", "isCompleted": True, "isCurrent": False,
             "isFuture": False, "date": "09/10/2026", "time": "12:13 P.M.",
             "location": "China"},
            {"name": "On the Way", "isCompleted": True, "isCurrent": True,
             "isFuture": False, "date": "09/14/2026", "time": "4:00 P.M.",
             "location": "Riga, Latvia"},
            {"name": "Delivery", "isCompleted": False, "isCurrent": False,
             "isFuture": True, "date": "", "time": "", "location": ""}],
        "shipmentProgressActivities": [
            {"date": "09/14/2026", "time": "4:00 P.M.", "location": "Riga, Latvia",
             "activityScan": "Held until duties and taxes are paid.",
             "milestoneName": {"name": "On the Way"}, "actCode": "OQ",
             "exceptionCodes": "OQ-Z2-null", "gmtDate": "20260914",
             "gmtTime": "13:00:00", "gmtOffset": "+03:00", "isBrokerageEvent": True},
            {"date": "09/14/2026", "time": "10:16 A.M.", "location": "Riga, Latvia",
             "activityScan": "Import Scan", "actCode": "IP", "gmtDate": "20260914",
             "gmtTime": "07:16:03", "gmtOffset": "+03:00"}],
    }]}

    result = ups_track.parse_tracking_response("1Z999AA10123456784", payload)

    assert result["source"] == "ups.com"
    assert result["estimated_delivery"] == {
        "local_date_text": "2026-09-15", "local_time_text": "14:00:00",
        "timezone_offset": "+03:00", "from_utc": "2026-09-15T11:00:00Z"}
    assert result["latest_event"]["status"] == "On the Way"
    assert result["events"][0]["occurred_at_utc"] == "2026-09-14T13:00:00Z"
    assert result["events"][0]["description"] == "Held until duties and taxes are paid."
    assert result["events"][0]["is_brokerage"] is True
    assert [step["name"] for step in result["progress_steps"]] == [
        "Label Created", "On the Way", "Delivery"]


def test_ups_compact_gmt_time_is_validated_before_marking_utc():
    assert ups_track._ups_utc("20260914", "130000") == "2026-09-14T13:00:00Z"
    assert ups_track._ups_utc("bad", "time") == "N/A"


def test_ups_official_not_found_is_not_an_unknown_status():
    payload = {"trackDetails": [{
        "trackingNumber": "1Z999AA10123456784",
        "errorCode": "504",
        "errorText": "Tracking number not found in database",
    }]}

    result = ups_track.parse_tracking_response("1Z999AA10123456784", payload)

    assert result["ok"] is False
    assert result["not_found"] is True
    assert result["error"] == "UPS tracking number not found"


def test_ups_does_not_retry_official_not_found(monkeypatch):
    calls = []
    monkeypatch.setattr(ups_track, "_track_once", lambda *_args: calls.append(1) or {
        "tracking": "1Z999AA10123456784", "ok": False,
        "not_found": True, "error": "UPS tracking number not found",
    })

    result = ups_track.track_ups("1Z999AA10123456784", attempts=2)

    assert result["not_found"] is True
    assert len(calls) == 1


def test_ups_drains_response_arriving_at_wait_deadline(monkeypatch):
    payload = {"trackDetails": [{
        "trackingNumber": "1Z999AA10123456784",
        "packageStatusType": "D", "packageStatus": "Delivered",
    }]}
    response = _LateResponse("https://webapis.ups.com/track/api/Track/GetStatus", payload)
    page = _LateResponsePage(response)
    _install_fake_browser(monkeypatch, page)
    clock = iter((0, 0, 2))
    monkeypatch.setattr(ups_track.time, "time", lambda: next(clock))

    result = ups_track._track_once("1Z999AA10123456784", 1, 1000, None)

    assert result["ok"] is True
    assert result["stage"] == "签收"


def test_dhl_reports_official_response_parse_failure(monkeypatch):
    import dhl_track
    response = _LateResponse(
        "https://www.dhl.com/utapi?trackingNumber=1234567890",
        ValueError("invalid JSON"))
    page = _LateResponsePage(response)
    _install_fake_browser(monkeypatch, page)
    clock = iter((0, 0, 2))
    monkeypatch.setattr(dhl_track.time, "time", lambda: next(clock))

    result = dhl_track.track_dhl("1234567890", timeout_nav=1, wait_ms=1000)

    assert result["error"] == "DHL response parse failed: ValueError"


def test_ups_retries_with_a_fresh_browser_after_missing_response(monkeypatch):
    calls = []
    monkeypatch.setattr(ups_track.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ups_track, "_track_once", lambda *_args: calls.append(1) or (
        {"tracking": "1Z999AA10123456784", "ok": False, "error": "no GetStatus data"}
        if len(calls) == 1 else
        {"tracking": "1Z999AA10123456784", "ok": True, "stage": "运输中"}
    ))

    result = ups_track.track_ups("1Z999AA10123456784", attempts=2)

    assert result["ok"] is True
    assert len(calls) == 2


def test_fedex_uses_official_page_without_api_credentials(monkeypatch):
    import fedex_track
    monkeypatch.setenv("FEDEX_CLIENT_ID", "client")
    monkeypatch.setenv("FEDEX_CLIENT_SECRET", "secret")
    calls = []
    monkeypatch.setattr(fedex_track.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(fedex_track, "_track_once", lambda *_args: calls.append(1) or (
        {"tracking": "876543210123", "ok": False, "error": "official site access denied"}
        if len(calls) == 1 else
        {"tracking": "876543210123", "ok": True, "stage": "运输中"}))

    assert fedex_track.track_fedex("876543210123")["ok"] is True
    assert len(calls) == 2


def test_fedex_dom_status_parsed_when_api_blocked():
    from fedex_track import parse_dom_status, page_failure

    body = ("DELIVERED\nThursday, 9/10/2026 at 10:45 AM\nMEMPHIS, TN US\n"
            "Package delivered to recipient")
    result = parse_dom_status("876543210123", body)

    assert result["ok"] is True
    assert result["stage"] == "签收"
    assert result["source"] == "dom"
    assert "MEMPHIS" in result["detail"]

    # 403 on the API endpoint alone no longer hides rendered page data
    assert page_failure("876543210123", [403], body)["ok"] is True


def test_fedex_dom_status_prefers_heading_over_scan_history():
    from fedex_track import dom_status_hint

    body = ("DELIVERED\nWednesday, 9/9/2026\nANCHORAGE, AK\n"
            "IN TRANSIT\nTuesday, 9/8/2026\nMEMPHIS, TN")

    assert dom_status_hint(body) == ("签收", "DELIVERED")
    assert dom_status_hint("no carrier text here") is None
    assert dom_status_hint("") is None


def test_fedex_page_failure_distinguishes_not_found_from_access_denied():
    from fedex_track import page_failure

    assert page_failure("123", [403], "")["error"] == "FedEx official site access denied (HTTP 403)"
    result = page_failure("123", [], "We can’t find that tracking number.")
    assert result["not_found"] is True


def test_fedex_dom_success_wins_over_late_api_parse_error(monkeypatch):
    import fedex_track
    response = _LateResponse(
        "https://api.fedex.com/track/v2/shipments", ValueError("invalid JSON"))
    page = _LateResponsePage(response, "DELIVERED\nMEMPHIS, TN US", send_on_goto=True)
    _install_fake_browser(monkeypatch, page)
    clock = iter((0, 0, 2))
    monkeypatch.setattr(fedex_track.time, "time", lambda: next(clock))

    result = fedex_track._track_once("876543210123", 1, 1000, None)

    assert result["ok"] is True
    assert result["stage"] == "签收"
    assert result["source"] == "dom"
