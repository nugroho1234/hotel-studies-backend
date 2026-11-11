import pandas as pd

from hotel_study.hotels import build_rows

COLS = ["hotel_id", "date_scraped", "destination", "hotel_name", "star_class", "num_reviews", "review_url", "address"]


def url(i):
    return f"https://x/Hotel_Review-g1-d{i}-Reviews-A.html"


def frame(*rows):
    return pd.DataFrame([dict(zip(COLS, ["", "2026-11-07", "bali", "H", "5", "50", "", ""])) | r for r in rows], dtype=str)


def test_duplicate_ids_different_star_flagged():
    rows, counts = build_rows(frame({"review_url": url(1), "star_class": "5"}, {"review_url": url(1), "star_class": "4"}), 100)
    assert len(rows) == 1 and rows[0]["star_class"] == 5
    assert rows[0]["star_check_status"] == "needs_manual_check"
    assert counts["duplicates_merged"] == 1


def test_duplicate_same_star_not_flagged():
    rows, _ = build_rows(frame({"review_url": url(1)}, {"review_url": url(1)}), 100)
    assert len(rows) == 1 and "star_check_status" not in rows[0]


def test_no_id_and_bad_values_skipped():
    rows, counts = build_rows(frame({"review_url": "nope"}, {"review_url": url(2), "star_class": "x"},
                                    {"review_url": url(3), "num_reviews": "abc"}, {"review_url": url(4)}), 100)
    assert [r["hotel_id"] for r in rows] == [4]
    assert counts["skipped_no_id"] == 1 and counts["skipped_bad_value"] == 2


def test_ge_threshold_and_blank_cells():
    rows, _ = build_rows(frame({"review_url": url(1), "num_reviews": "99"}, {"review_url": url(2), "num_reviews": "100"},
                                {"review_url": url(3), "num_reviews": "", "address": ""}), 100)
    assert [r["num_reviews_ge_100"] for r in rows] == [False, True, False]
    assert rows[2]["num_reviews"] is None


def test_limit_and_destination():
    f = frame({"review_url": url(1), "destination": "Bali"}, {"review_url": url(2), "destination": "jakarta"},
              {"review_url": url(3), "destination": "bali"})
    rows, _ = build_rows(f, 100, limit=1, destination="bali")
    assert [r["hotel_id"] for r in rows] == [1]
