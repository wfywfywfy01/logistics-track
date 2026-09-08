from dhl_track import classify_status as classify_dhl
from ups_track import classify_status as classify_ups


def test_unknown_ups_status_is_not_guessed_as_in_transit():
    assert classify_ups("Z", "A newly introduced carrier state") is None


def test_unknown_dhl_status_is_not_guessed_as_in_transit():
    assert classify_dhl("brand_new", "A newly introduced carrier state") is None


def test_known_carrier_statuses_are_classified():
    assert classify_ups("D", "Delivered") == "签收"
    assert classify_dhl("transit", "Departed facility") == "运输中"
