import hashlib
import json
import logging
from datetime import date

from hotel_study.privacy import flatten_review, hash_user_id, in_window
from hotel_study.reviews import ingest_items, split_items

from hotel_study.config import load_config

CFG = {"subrating_labels": load_config()["subrating_labels"], "review_window": {"start": "2023-01-01", "end": "2025-12-31"}, "languages": ["en", "id"]}
START, END = "2023-01-01", "2025-12-31"


def item(**kw):
    base = {
        "id": 111, "location": {"locationId": 7}, "createdDate": "2025-03-02T10:00:00Z",
        "publishedDate": "2025-03-03T10:00:00Z", "publishPlatform": "OTHER", "rating": 4,
        "language": "en", "originalLanguage": "en", "translationType": None, "title": "Nice",
        "text": "Good stay", "url": "https://x/r", "helpfulVotes": 2, "mcid": 5,
        "tripInfo": {"stayDate": "2025-03", "tripType": "COUPLES"},
        "additionalRatings": [{"ratingLabel": "Value", "rating": 5}, {"ratingLabel": "Sleep Quality", "rating": 3},
                              {"ratingLabel": "Weirdness", "rating": 2}],
        "photos": [{"image": "https://p/1.jpg"}, {"image": "https://p/2.jpg"}],
        "userProfile": {"userId": "U1", "displayName": "Jane Doe", "username": "janed99",
                        "avatar": "https://a/jane.jpg", "route": "/Profile/janed99", "hometown": "Jakarta",
                        "contributionCounts": {"sumAllUgc": 12}},
        "ownerResponse": {"id": 9, "publishedDate": "2025-03-05T00:00:00Z", "language": "en",
                          "text": "Thanks Jane", "connectionToSubject": "Manager",
                          "username": "madeG", "responder": "Made Gunawan"},
        "inputSource": {"pageNumber": 3},
    }
    base.update(kw)
    return base


def test_hash():
    assert hash_user_id("abc", "salt") == hashlib.sha256(b"saltabc").hexdigest()


def test_no_pii_and_fields():
    r = flatten_review(item(), "s")
    blob = json.dumps(r, default=str)
    for bad in ["Jane Doe", "janed99", "a/jane.jpg", "/Profile", "madeG", "Made Gunawan", "p/1.jpg"]:
        assert bad not in blob
    assert not {"responder", "username", "displayName", "avatar", "route"} & set(r)
    assert not any("responder" in k or "username" in k for k in r)
    assert r["photo_count"] == 2 and r["user_id_hash"] == hash_user_id("U1", "s")
    assert r["review_id"] == 111 and r["hotel_id"] == 7 and r["published_date"] == date(2025, 3, 3)
    assert r["subrating_value"] == 5 and r["subrating_sleep_quality"] == 3
    assert r["subratings_other"] == {"Weirdness": 2}
    assert r["trip_info_stay_date"] == date(2025, 3, 1) and r["user_contribution_count"] == 12
    assert r["owner_response_published_date"] == date(2025, 3, 5) and r["input_page_number"] == 3


def test_missing_optional_blocks():
    r = flatten_review({"id": 1, "location": {"locationId": 2}, "publishedDate": "2025-01-01T00:00:00Z"}, "s")
    assert r["photo_count"] == 0 and r["user_id_hash"] is None and r["owner_response_text"] is None
    assert r["trip_info_stay_date"] is None and r["subratings_other"] is None
    r = flatten_review(item(userProfile=None, ownerResponse=None, tripInfo=None, photos=None, additionalRatings=None), "s")
    assert r["photo_count"] == 0


def test_stay_date_tolerant():
    for v, exp in [("2025-03-01", date(2025, 3, 1)), ("2025-03", date(2025, 3, 1)), ("junk", None)]:
        assert flatten_review(item(tripInfo={"stayDate": v}), "s")["trip_info_stay_date"] == exp


def test_window():
    assert in_window("2025-12-31T23:59:00Z", START, END) is True
    assert in_window("2026-01-01T00:00:00Z", START, END) is False
    assert in_window("2023-01-01", START, END) is True
    assert in_window("2022-12-31", START, END) is False


def test_owner_text_names_removed():
    o = {"text": "Dear Jane, thanks. MADE GUNAWAN here, madeg too.", "responder": "Made Gunawan", "username": "madeG"}
    t = flatten_review(item(ownerResponse=o), "s")["owner_response_text"]
    assert "gunawan" not in t.lower() and "madeg" not in t.lower()


def test_signature_removed_and_plain_unchanged():
    def txt(s):
        return flatten_review(item(ownerResponse={"text": s}), "s")["owner_response_text"]
    assert txt("Thank you.\nBest regards,\nMade\nGuest Relations Manager") == "Thank you."
    assert txt("Thank you.\nTerima kasih\nMade") == "Thank you."
    assert txt("Thanks for staying. Regards are due.\nSee you.") == "Thanks for staying. Regards are due.\nSee you."
    assert txt("Regards\na\nb\nc\nd") == "Regards\na\nb\nc\nd"  # phrase not in last 3 lines


def test_split_items_counts_and_flags(caplog):
    items = [item(id=1), item(id=2, publishedDate="2026-01-01T00:00:00Z"), item(id=3, publishedDate="2022-12-31"),
             item(id=4, language="fr", originalLanguage="fr"), item(id=5, translationType="MACHINE")]
    with caplog.at_level(logging.ERROR):
        rows, counts = split_items(items, CFG, "s")
    assert [r["review_id"] for r in rows] == [1, 4, 5]
    assert [r["lang_ok"] for r in rows] == [True, False, False]
    assert counts[7] == {"n_raw": 5, "n_out_of_window": 2, "n_other_language": 2, "n_eligible": 1, "n_bad_date": 0}
    assert "translation_type" in caplog.text


def test_ingest_items_thin_write(monkeypatch):
    calls = []
    monkeypatch.setattr("hotel_study.reviews.get_secret", lambda n: "s")
    monkeypatch.setattr("hotel_study.reviews.db.upsert", lambda *a: calls.append(a) or len(a[2]))
    out = ingest_items("conn", "run1", [item(id=1), item(id=2, publishedDate="2026-02-01T00:00:00Z")], CFG)
    assert out[7]["n_out_of_window"] == 1
    (_, table, rows, key), = calls
    assert table == "reviews" and key == "review_id" and rows[0]["scrape_run_id"] == "run1" and len(rows) == 1


def _own(s, **o):
    return flatten_review(item(ownerResponse={"text": s, **o}), "s")["owner_response_text"]


def test_closing_phrase_must_be_whole_line():
    assert _own("Terima kasih atas ulasan Anda.") == "Terima kasih atas ulasan Anda."
    assert _own("Terima kasih,\nMade\nGuest Relations") == ""
    assert _own("Thanks.\nTerima kasih,\nMade\nGuest Relations") == "Thanks."
    t = "Salam hangat dari kami di Bali, semoga kembali.\nKami tunggu.\nSampai jumpa."
    assert _own(t) == t
    assert _own("Thanks.\nSalam\nMade\n\n\n") == "Thanks."  # trailing blank lines ignored


def test_name_word_boundaries():
    assert _own("We made it homemade. Made says hi.", responder="Made") == "We  it homemade.  says hi."  # case-insensitive, whole word only
    assert _own("Hi Al", responder="Al") == "Hi Al"  # names under 3 chars skipped


def test_malformed_items_skipped_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        rows, counts = split_items([item(id=None), item(location={}), item(id=8, publishedDate=None), item(id=9)], CFG, "s")
    assert [r["review_id"] for r in rows] == [9]
    assert None not in counts and counts[7]["n_bad_date"] == 1 and counts[7]["n_out_of_window"] == 0
    assert counts[7]["n_raw"] == 2


def _sub(labels):
    return flatten_review(item(additionalRatings=[{"ratingLabel": k, "rating": v} for k, v in labels]), "s")


def test_indonesian_subrating_labels():
    r = _sub([("Layanan", 1), ("Kualitas Tidur", 2), ("Lokasi", 3), ("Kamar", 4), ("Kebersihan", 5), ("Nilai", 2)])
    assert (r["subrating_service"], r["subrating_sleep_quality"], r["subrating_location"], r["subrating_rooms"],
            r["subrating_cleanliness"], r["subrating_value"]) == (1, 2, 3, 4, 5, 2)
    assert r["subratings_other"] is None


def test_english_unknown_and_messy_subrating_labels():
    r = _sub([("Value", 5), ("  SLEEP quality ", 3), ("kebersihan", 4), ("Foo", 2)])
    assert r["subrating_value"] == 5 and r["subrating_sleep_quality"] == 3 and r["subrating_cleanliness"] == 4
    assert r["subrating_rooms"] is None and r["subratings_other"] == {"Foo": 2}


def test_foreign_location_dropped_and_logged(caplog):
    items = [item(id=1), item(id=2, location={"locationId": 99}), item(id=3, location={"locationId": 7})]
    with caplog.at_level(logging.ERROR):
        rows, counts = split_items(items, CFG, "s", hotel_ids={7})
    assert [r["review_id"] for r in rows] == [1, 3] and set(counts) == {7} and counts[7]["n_raw"] == 2
    assert "99" in caplog.text and "Good stay" not in caplog.text


def test_ingest_items_drops_foreign_before_upsert(monkeypatch):
    seen = []
    monkeypatch.setattr("hotel_study.reviews.get_secret", lambda n: "s")
    monkeypatch.setattr("hotel_study.reviews.db.upsert", lambda c, t, rows, k: seen.extend(rows))
    ingest_items("conn", "run1", [item(id=1), item(id=2, location={"locationId": 99})], CFG, hotel_ids={7})
    assert [r["review_id"] for r in seen] == [1]


def test_no_rating_never_eligible():
    rows, counts = split_items([item(id=1, rating=None), item(id=2)], CFG, "s")
    assert [r["lang_ok"] for r in rows] == [False, True]
    assert counts[7]["n_eligible"] == 1 and counts[7]["n_raw"] == 2 and counts[7]["n_other_language"] == 1


def test_duplicate_review_ids_counted_once_first_wins():
    rows, counts = split_items([item(id=1, title="first"), item(id=1, title="second"), item(id=2)], CFG, "s")
    assert [(r["review_id"], r["title"]) for r in rows] == [(1, "first"), (2, "Nice")]
    assert counts[7]["n_raw"] == 2 and counts[7]["n_eligible"] == 2


def test_string_location_id_is_normalised(caplog):
    rows, counts = split_items([item(id=1, location={"locationId": "7"}), item(id=2)], CFG, "s", hotel_ids={7})
    assert [r["hotel_id"] for r in rows] == [7, 7] and counts[7]["n_raw"] == 2


def test_non_numeric_location_id_skipped_and_logged(caplog):
    with caplog.at_level(logging.ERROR):
        rows, counts = split_items([item(id=5, location={"locationId": "abc"}), item(id=2)], CFG, "s", hotel_ids={7})
    assert [r["review_id"] for r in rows] == [2] and "5" in caplog.text and "Good stay" not in caplog.text


def test_foreign_int_and_str_ids_still_dropped():
    rows, counts = split_items([item(id=1, location={"locationId": "99"}), item(id=2, location={"locationId": 98}), item(id=3)], CFG, "s", hotel_ids={7})
    assert [r["review_id"] for r in rows] == [3] and set(counts) == {7}


def test_negative_helpful_votes_stored_as_null():
    row = flatten_review(item(helpfulVotes=-1), "salt", CFG["subrating_labels"])
    assert row["helpful_votes"] is None
    assert flatten_review(item(helpfulVotes=0), "salt", CFG["subrating_labels"])["helpful_votes"] == 0
