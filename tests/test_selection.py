from datetime import date, timedelta

from hotel_study.selection import select_for_hotel, select_reviews


def rv(i, d=None, lang="en", tt=None):
    return {"review_id": i, "published_date": d or date(2023, 1, 1) + timedelta(days=i), "language": lang, "translation_type": tt}


def test_400_eligible_selects_300_newest_first():
    out = select_for_hotel([rv(i) for i in range(400)], 300)
    sel = sorted((r for r in out if r["is_selected"]), key=lambda r: r["selection_rank"])
    assert [r["selection_rank"] for r in sel] == list(range(1, 301))
    assert [r["review_id"] for r in sel] == list(range(399, 99, -1))
    assert all(r["selection_rank"] is None for r in out if not r["is_selected"])


def test_same_date_higher_review_id_first():
    d = date(2024, 5, 5)
    out = {r["review_id"]: r for r in select_for_hotel([rv(1, d), rv(9, d), rv(5, d)], 300)}
    assert (out[9]["selection_rank"], out[5]["selection_rank"], out[1]["selection_rank"]) == (1, 2, 3)


def test_fewer_than_n_all_selected():
    out = select_for_hotel([rv(i) for i in range(120)])
    assert all(r["is_selected"] for r in out) and len(out) == 120


def test_other_language_and_translation_never_selected():
    out = {r["review_id"]: r for r in select_for_hotel([rv(1, lang="fr"), rv(2, tt="machine"), rv(3), rv(4, lang="id")], 300)}
    for i in (1, 2):
        assert out[i]["lang_ok"] is False and not out[i]["is_selected"] and out[i]["selection_rank"] is None
    assert out[3]["lang_ok"] and out[4]["lang_ok"]


def test_out_of_window_never_selected():
    out = {r["review_id"]: r for r in select_for_hotel([rv(1, date(2026, 1, 2)), rv(2, date(2022, 12, 31)), rv(3)], 300)}
    assert not out[1]["is_selected"] and not out[2]["is_selected"] and out[3]["is_selected"]


def test_idempotent():
    revs = [rv(i, lang="fr" if i % 7 == 0 else "en") for i in range(50)]
    a = select_for_hotel(revs, 20)
    assert a == select_for_hotel(revs, 20) == select_for_hotel([dict(r) for r in a], 20)


class FakeConn:
    def __init__(self, hotels, reviews):
        self.hotels, self.reviews, self.log, self.committed = hotels, reviews, [], False

    def execute(self, q, params=None):
        q = str(q)
        self.log.append((q, params))
        if "FROM hotels" in q and q.lstrip().startswith("SELECT"):
            self._rows = self.hotels
        elif "FROM reviews" in q and q.lstrip().startswith("SELECT"):
            self._rows = self.reviews[params[0]]
        else:
            self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def commit(self):
        self.committed = True


def test_select_reviews_only_final_complete_resets_rank_first_and_counts():
    revs = {1: [(i, date(2024, 1, 1) + timedelta(days=i), "en", None) for i in range(5)] + [(99, date(2024, 1, 1), "fr", None)]}
    c = FakeConn([(1, "Bali")], revs)
    select_reviews(conn=c)
    sql = [q for q, _ in c.log]
    hq = next(q for q in sql if "FROM hotels" in q and q.lstrip().startswith("SELECT"))
    assert "in_final_sample" in hq and "'complete'" in hq
    assert any("is_selected = false" in q and "NOT in_final_sample" in q for q in sql)
    i_null = next(i for i, q in enumerate(sql) if "selection_rank = NULL" in q and params_is(c, i, 1))
    i_set = next(i for i, q in enumerate(sql) if "SET lang_ok" in q)
    assert i_null < i_set
    sets = [p for q, p in c.log if "SET lang_ok" in q]
    assert len(sets) == 6 and sum(1 for p in sets if p[1]) == 5
    assert (5, 1) in [p for q, p in c.log if "n_eligible_reviews" in q]
    assert c.committed


def params_is(c, i, hid):
    return c.log[i][1] == (hid,)
