from hotel_study.matching import build_query, classify_match, match_score, normalise_name, pick_best

CFG = {"accept": 90, "uncertain": 75}


def test_classify_boundaries():
    assert [classify_match(s, CFG) for s in (90, 89, 75, 74)] == ["matched", "uncertain", "uncertain", "none"]


def test_normalise_and_score():
    assert normalise_name("The Alila, Seminyak Hotel by Hyatt") == "alila seminyak hyatt"
    assert match_score("Hotel Santika Jakarta", "Santika Jakarta") == 100


def test_build_query():
    assert build_query("Hotel Santika Jakarta", "jakarta") == "Hotel Santika Jakarta"
    assert build_query("Alila Seminyak", "bali") == "Alila Seminyak Bali"


def test_pick_best_of_three():
    res = [{"name": "Other Place", "star_rating": 3}, {"name": "Alila Seminyak", "star_rating": 5, "detail_url": "u"},
           {"name": "Alila", "star_rating": 4}]
    b = pick_best("Alila Seminyak", res, CFG)
    assert b["google_name"] == "Alila Seminyak" and b["google_star_rating"] == 5 and b["google_match_score"] == 100


def test_pick_best_below_uncertain_has_no_name():
    b = pick_best("Alila Seminyak", [{"name": "Zzz Qqq", "star_rating": 5}], CFG)
    assert b["google_name"] is None and b["google_star_rating"] is None and b["google_match_score"] < 75


def test_no_results():
    assert pick_best("X", [], CFG)["google_match_score"] == 0


def test_star_fallback():
    r = lambda **k: pick_best("Alila", [{"name": "Alila"} | k], CFG)["google_star_rating"]
    assert r(star_rating=4, star_class="5-star hotel") == 4
    assert r(star_class="5-star hotel") == 5
    assert r() is None
