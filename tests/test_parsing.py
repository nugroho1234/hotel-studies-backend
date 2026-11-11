from hotel_study.parsing import parse_hotel_id, parse_star_label


def test_parse_hotel_id():
    assert parse_hotel_id("https://x/Hotel_Review-g294226-d14942464-Reviews-X.html") == 14942464
    assert parse_hotel_id("https://x/Hotel_Review-g294226-Reviews-X.html") is None
    assert parse_hotel_id("") is None and parse_hotel_id(None) is None


def test_parse_star_label():
    assert parse_star_label("5-star hotel") == 5
    assert parse_star_label("4.5-star hotel") == 4
    assert parse_star_label(None) is None
    assert parse_star_label("resort") is None
