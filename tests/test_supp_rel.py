import pandas as pd

from hotel_study.supp_rel import draw_supp, kw_regex, load_keywords


def test_kw_regex_wildcard_and_word_boundary():
    assert kw_regex("overbook*").search("They OVERBOOKED the hotel")
    assert kw_regex("bill").search("the bill was wrong") and not kw_regex("bill").search("billing")
    assert kw_regex("not ready").search("room was Not  ready") and not kw_regex("promised").search("unpromised")
    assert kw_regex("tdk sesuai").search("Kamar tdk sesuai foto")


def test_keywords_load_both_languages():
    kw = load_keywords()
    assert "refund*" in kw["en"] and "sesuai gambar" in kw["id"]


def _df(n_id, n_en):
    rows = [(i, "id" if i < n_id else "en", 3 + i % 3) for i in range(n_id + n_en)]
    return pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating"])


def test_draw_counts_and_split():
    out, info = draw_supp(_df(500, 500), 42, 100, 25)
    v = pd.Series(out).value_counts()
    assert info == {"total": 100, "id": 50} and v["supp_rel_dev"] == 25 and v["supp_rel_test"] == 75


def test_indonesian_shortfall_filled_and_total_short():
    out, info = draw_supp(_df(20, 500), 42, 100, 25)
    assert info == {"total": 100, "id": 20}
    out, info = draw_supp(_df(20, 30), 42, 100, 25)
    assert info["total"] == 50 and len(out) == 50
