import pytest

from hotel_study.stars import OpenManualChecks, plan_finalise, resolve_manual, star_status

T = 90


@pytest.mark.parametrize("google,ta,score,want", [
    (None, 4, 80, ("match_uncertain", None)),   # row 1 beats google null
    (5, 4, 89.9, ("match_uncertain", None)),    # row 1 beats +1
    (None, 4, 95, ("google_missing", 4)),
    (None, 4, None, ("google_missing", 4)),     # no Google hit, no score
    (4, 4, 90, ("agree", 4)),
    (5, 4, 100, ("needs_manual_check", None)),
    (3, 4, 100, ("disagree", None)),
    (6, 4, 100, ("disagree", None)),            # +2
])
def test_star_status_table(google, ta, score, want):
    assert star_status(google, ta, score, T) == want


def row(**kw):
    base = dict(star_class=4, google_star_rating=5, match_confirmed="", manual_google_star="", ta_displayed_class="")
    return base | kw


def test_resolve_match_yes_diff_zero_needs_no_ta():
    assert resolve_manual(row(google_star_rating=4, match_confirmed="y")) == ("agree", 4)


def test_resolve_manual_star_replaces_google():
    assert resolve_manual(row(match_confirmed="n", manual_google_star="4")) == ("agree", 4)
    assert resolve_manual(row(match_confirmed="n", manual_google_star="2")) == ("disagree", None)


def test_resolve_no_without_manual_is_google_missing():
    assert resolve_manual(row(match_confirmed="n")) == ("google_missing", 4)


def test_half_star():
    assert resolve_manual(row(ta_displayed_class="4.5")) == ("agree_half_star", 4)
    assert resolve_manual(row(ta_displayed_class="4.0")) == ("disagree", None)
    assert resolve_manual(row(ta_displayed_class="4")) == ("disagree", None)


@pytest.mark.parametrize("bad", [
    row(match_confirmed="y", ta_displayed_class=""),    # +1, blank ta
    row(match_confirmed="n", manual_google_star="9"),
    row(match_confirmed="maybe"),
    row(ta_displayed_class="4.3"),
    row(ta_displayed_class="x"),
])
def test_resolve_invalid_or_open_raises(bad):
    with pytest.raises(ValueError):
        resolve_manual(bad)


def test_uncertain_blank_match_is_open():
    with pytest.raises(ValueError):
        resolve_manual(row(), uncertain=True)


def hotel(**kw):
    return dict(star_class=4, google_star_rating=4, google_match_score=100.0, num_reviews_ge_100=True,
                star_check_status="agree", google_scraped=True) | kw


def test_plan_finalise_happy_path():
    hotels = {1: hotel(google_match_score=80.0, star_check_status="match_uncertain"),
              2: hotel(google_star_rating=5, star_check_status="needs_manual_check"),
              3: hotel(), 4: hotel(google_star_rating=2, star_check_status="disagree"),
              5: hotel(num_reviews_ge_100=False, star_check_status=None, google_scraped=False, google_star_rating=None)}
    csv = [dict(hotel_id="1", match_confirmed="y", manual_google_star="", ta_displayed_class=""),
           dict(hotel_id="2", match_confirmed="", manual_google_star="", ta_displayed_class="4.5")]
    out = {u["hotel_id"]: u for u in plan_finalise(csv, hotels, set(hotels), T)}
    assert out[1]["star_check_status"] == "agree" and out[1]["final_star_rating"] == 4 and out[1]["star_diff"] == 0
    assert out[2]["star_check_status"] == "agree_half_star" and out[2]["final_star_rating"] == 4 and out[2]["ta_displayed_class"] == 4.5
    assert 3 not in out and 4 not in out and 5 not in out  # untouched by the CSV


def test_plan_finalise_refuses_with_all_problems_listed():
    hotels = {1: hotel(google_match_score=80.0, star_check_status="match_uncertain"),
              2: hotel(google_star_rating=5, star_check_status="needs_manual_check"),
              3: hotel(google_match_score=80.0, star_check_status="match_uncertain"),
              4: hotel(google_match_score=80.0, star_check_status="match_uncertain")}
    csv = [dict(hotel_id="1", match_confirmed="", manual_google_star="", ta_displayed_class=""),
           dict(hotel_id="2", match_confirmed="", manual_google_star="", ta_displayed_class=""),
           dict(hotel_id="4", match_confirmed="n", manual_google_star="7", ta_displayed_class=""),
           dict(hotel_id="999", match_confirmed="y", manual_google_star="", ta_displayed_class="")]
    with pytest.raises(OpenManualChecks) as e:
        plan_finalise(csv, hotels, set(hotels), T)  # 3 is missing from the CSV; 999 not in DB
    assert {i for i, _ in e.value.items} == {1, 2, 3, 4, 999}
    assert all(str(i) in str(e.value) for i in (1, 2, 3, 4, 999))


def test_plan_finalise_refuses_unchecked_prescreened():
    hotels = {1: hotel(star_check_status=None), 2: hotel()}
    with pytest.raises(OpenManualChecks, match="1 pre-screened hotel.*not yet checked") as e:
        plan_finalise([], hotels, set(hotels), T)
    assert e.value.items == [(1, "pre-screened but not yet checked")]
    assert plan_finalise([], hotels, set(hotels), T, check_unchecked=False) == []  # scoped test run


COMPARE_COLS = dict(hotel_name="X", destination="bali", google_name="", star_diff="", star_check_status="match_uncertain")


def test_plan_finalise_with_csv_rows_shaped_like_compare_stars():
    hotels = {1: hotel(google_match_score=0.0, google_star_rating=None, star_check_status="match_uncertain"),
              2: hotel(google_match_score=80.0, google_star_rating=5, star_check_status="match_uncertain")}
    csv = [dict(COMPARE_COLS, hotel_id="1", star_class="4", google_star_rating="", match_confirmed="y", manual_google_star="", ta_displayed_class=""),
           dict(COMPARE_COLS, hotel_id="2", star_class="4", google_star_rating="5", star_diff="1", match_confirmed="y",
                manual_google_star="", ta_displayed_class="4.5")]
    out = {u["hotel_id"]: u for u in plan_finalise(csv, hotels, set(hotels), T)}
    assert out[1]["star_check_status"] == "google_missing" and out[1]["final_star_rating"] == 4 and out[1]["star_diff"] is None
    assert out[2]["star_check_status"] == "agree_half_star"


def test_plan_finalise_manual_star_reaching_plus_one_needs_ta():
    hotels = {1: hotel(google_match_score=80.0, google_star_rating=2, star_check_status="match_uncertain")}
    csv = [dict(hotel_id="1", star_class="4", match_confirmed="n", manual_google_star="5", ta_displayed_class="")]
    with pytest.raises(OpenManualChecks, match="ta_displayed_class"):
        plan_finalise(csv, hotels, set(hotels), T)
    csv[0]["ta_displayed_class"] = "4.0"
    assert plan_finalise(csv, hotels, set(hotels), T)[0]["star_check_status"] == "disagree"


def test_plan_finalise_hotel_missing_from_csv_is_listed():
    hotels = {1: hotel(google_match_score=80.0, star_check_status="match_uncertain")}
    with pytest.raises(OpenManualChecks) as e:
        plan_finalise([], hotels, set(hotels), T)
    assert e.value.items[0][0] == 1


def test_compare_refuses_to_overwrite_manual_work(tmp_path, monkeypatch):
    from hotel_study import stars
    monkeypatch.setattr(stars, "OUT", tmp_path)
    p = tmp_path / "manual_checks.csv"
    p.write_text("hotel_id,match_confirmed,manual_google_star,ta_displayed_class\n1,,,\n")
    assert not stars._has_manual_work(p)
    p.write_text("hotel_id,match_confirmed,manual_google_star,ta_displayed_class\n1,y,,\n")
    assert stars._has_manual_work(p)
    with pytest.raises(RuntimeError, match="--overwrite"):
        stars.compare_stars()  # refuses before touching the DB


def test_plan_finalise_outside_subset_ignored_but_unknown_reported():
    with pytest.raises(OpenManualChecks) as e:
        plan_finalise([dict(hotel_id="8", match_confirmed="y"), dict(hotel_id="abc")], {}, {7}, T)
    assert len(e.value.items) == 2
    assert plan_finalise([dict(hotel_id="7", match_confirmed="y")], {}, {7}, T) == []


def test_compare_passes_bool_in_sample_frame(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hotel_study import stars
    monkeypatch.setattr(stars, "OUT", tmp_path)
    monkeypatch.setattr(stars, "load_config", lambda: {"name_match": {"accept": 90}})
    cols = ["hotel_id", "hotel_name", "destination", "star_class", "google_name", "google_star_rating",
            "google_match_score", "google_scraped_at", "num_reviews_ge_100", "star_check_status"]
    recs = [(1, "A", "bali", 4, None, None, 0.0, "t", True, None), (2, "B", "bali", 4, "B", 4, 100.0, "t", True, None)]
    seen = []

    class Cur:
        description = [SimpleNamespace(name=c) for c in cols]
        def fetchall(self): return recs
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def executemany(self, q, params): seen.extend(params)

    class Conn(Cur):
        def execute(self, *a): return Cur()
        def cursor(self): return Cur()
        def commit(self): pass

    monkeypatch.setattr(stars, "connect", lambda: Conn())
    stars.compare_stars()
    got = {p["hotel_id"]: p for p in seen}
    assert got[1]["in_sample_frame"] is False and got[1]["final_star_rating"] is None
    assert got[2]["in_sample_frame"] is True and got[2]["final_star_rating"] == 4
