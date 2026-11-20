from hotel_study.google_ids import entity_url, extract_entity_id, extract_kg_id

U1 = "https://www.google.com/travel/hotels/entity/ChkI6ai6092K-sxqGg0vZy8xMXF5Xzd3Y2IzEAE"
U2 = "https://www.google.com/travel/hotels/entity/ChcIie6O26uqz_uVARoKL20vMHdmZDczcRAB"


def test_extract_entity_id():
    assert extract_entity_id(U1) == "ChkI6ai6092K-sxqGg0vZy8xMXF5Xzd3Y2IzEAE"
    assert extract_entity_id(U1 + "?hl=en&q=x") == "ChkI6ai6092K-sxqGg0vZy8xMXF5Xzd3Y2IzEAE"


def test_extract_entity_id_from_qs_fallback():
    assert extract_entity_id("https://www.google.com/travel/hotels?qs=ChkIabc") is None  # not a wrapped token


def test_bad_input_returns_none_never_raises():
    for bad in (None, "", "https://example.com/x", "https://www.google.com/travel/hotels?qs=%%%", "http://[bad"):
        assert extract_entity_id(bad) is None
        assert extract_kg_id(bad) is None


def test_kg_id_g_and_m():
    assert extract_kg_id(U1) == "/g/11qy_7wcb3"
    assert extract_kg_id(U2) == "/m/0wfd73q"


def test_kg_id_none_when_token_has_none():
    assert extract_kg_id("https://www.google.com/travel/hotels/entity/QUJD") is None  # "ABC"


def test_entity_url_roundtrip():
    assert entity_url("ChkIabc") == "https://www.google.com/travel/hotels/entity/ChkIabc"


TRAVEL = ("https://www.google.com/travel/search?qs=MidDaGtJd3RpSzVwMkpwN1Z6R2cwdlp5OHhNV0kzTTJwNk9EVTBFQUU4QUE"
          "&ved=0CAAQ5JsGahgKEwiYnP3hlJaYAxUAAAAAHQAAAAAQhQE&ts=CAEaHgoAEhoSFAoHCOoPEAsYHhIHCOoPEAwYARgBMgIIAioHCgU6A0lEUg")


def test_travel_search_url_with_wrapped_qs():
    assert extract_entity_id(TRAVEL) == "ChkIwtiK5p2Jp7VzGg0vZy8xMWI3M2p6ODU0EAE"
    assert extract_kg_id(TRAVEL) == "/g/11b73jz854"


def test_search_page_without_hotel_has_no_entity_id():
    assert extract_entity_id("https://www.google.com/travel/search?q=x&qs=CAAgASgA") is None


def test_new_style_qs_with_leading_fields():
    url = "https://www.google.com/travel/search?q=x&qs=CAAgASgAMihDaG9Jb0pmZzc5alM0N2pBQVJvTkwyY3ZNVEZxTjNJeGF6aDZNaEFC"
    assert extract_entity_id(url) == "ChoIoJfg79jS47jAARoNL2cvMTFqN3Ixazh6MhAB"
    assert extract_kg_id(url) == "/g/11j7r1k8z2"


def test_is_no_price():
    from hotel_study.google_ids import is_no_price
    assert is_no_price("NO_GOOGLE_PRICE") and is_no_price(" no_google_page ")
    assert not is_no_price("CLOSED") and not is_no_price(None) and not is_no_price(float("nan"))
