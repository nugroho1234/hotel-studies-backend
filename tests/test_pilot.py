import random

import pandas as pd
import pytest

from hotel_study.config import load_config
from hotel_study.pilot import summarise, EXPORT_COLS, SELECT_COLS, allocate, draw_pilot, pilot_sample


def test_allocate_equal_remainder_to_largest():
    sizes = {f"s{i}": 500 for i in range(9)}
    sizes["s3"] = 900
    a = allocate(sizes, 100)
    assert sum(a.values()) == 100 and a["s3"] == 12 and sorted(a.values()) == [11] * 8 + [12]


def test_allocate_shortfall_goes_to_others():
    sizes = {f"s{i}": 500 for i in range(9)}
    sizes["s0"] = 5
    a = allocate(sizes, 100)
    assert a["s0"] == 5 and sum(a.values()) == 100 and max(a.values()) <= 12


def test_allocate_pool_too_small():
    assert allocate({"a": 3, "b": 4}, 100) == {"a": 3, "b": 4}


def make_df(seed=1, n_hotels_per=4, per_hotel=40, id_share=0.3):
    rng = random.Random(seed)
    rows, rid, hid = [], 1, 1
    for d in ("Bali", "Jakarta", "Surabaya"):
        for star in (3, 4, 5):
            for _ in range(n_hotels_per):
                for _ in range(per_hotel):
                    rows.append({"review_id": rid, "hotel_id": hid, "destination": d, "final_star_rating": star,
                                 "language": "id" if rng.random() < id_share else "en"})
                    rid += 1
                hid += 1
    return pd.DataFrame(rows)


def test_draw_respects_cap_total_no_dupes_and_strata():
    df = make_df(n_hotels_per=6)
    out = draw_pilot(df, 42, 2, 20)
    assert len(out) == 100 and out.review_id.is_unique
    assert out.groupby("hotel_id").size().max() <= 2
    assert sorted(out.groupby("pilot_stratum").size().tolist()) == [11] * 8 + [12]


def test_min_id_met_when_available_even_if_rare():
    df = make_df(id_share=0.03)
    assert (draw_pilot(df, 42, 2, 20).language == "id").sum() >= 20


def test_fewer_id_only_when_fewer_exist():
    df = make_df(id_share=0.0)
    df.loc[df.index[::40][:7], "language"] = "id"
    out = draw_pilot(df, 42, 4, 20)
    assert (out.language == "id").sum() == 7 and len(out) == 100


def test_cap_limits_total_when_few_hotels():
    df = make_df(n_hotels_per=1)  # 9 hotels, cap 4 -> 36 max
    out = draw_pilot(df, 42, 4, 20)
    assert len(out) == 36 and out.groupby("hotel_id").size().max() == 4


def test_deterministic_and_input_order_independent():
    df = make_df()
    a = set(draw_pilot(df, 42, 4, 20).review_id)
    assert a == set(draw_pilot(df.sample(frac=1, random_state=3), 42, 4, 20).review_id)
    assert a != set(draw_pilot(df, 7, 4, 20).review_id)


class FakeConn:
    def __init__(self, pilot_exists, rows):
        self.pilot_exists, self.rows, self.log, self.commits = pilot_exists, rows, [], 0

    def execute(self, q, params=None):
        q = str(q)
        self.log.append(q)
        if "is_pilot" in q and q.lstrip().startswith("SELECT 1"):
            self._rows = [(1,)] if self.pilot_exists else []
        elif q.lstrip().startswith("SELECT"):
            self._rows = self.rows
        else:
            self._rows = []
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def commit(self):
        self.commits += 1


def db_rows(n_hotels=2, per=10):
    out = []
    for h in range(n_hotels):
        for i in range(per):
            d = dict.fromkeys(SELECT_COLS)
            d.update(review_id=h * 100 + i, hotel_id=h, hotel_name=f"H{h}", destination="Bali", final_star_rating=4,
                     language="id" if i % 2 else "en", title="t", text="x")
            out.append(tuple(d[c] for c in SELECT_COLS))
    return out


def test_refuses_without_force_when_pilot_exists(tmp_path):
    c = FakeConn(True, db_rows())
    with pytest.raises(RuntimeError, match="--force"):
        pilot_sample(False, conn=c, out_dir=tmp_path)
    assert not any(q.lstrip().startswith(("UPDATE", "SELECT r.review_id")) for q in c.log) and c.commits == 0


def test_force_clears_old_flags_first_then_sets_and_exports(tmp_path):
    c = FakeConn(True, db_rows())
    msg = pilot_sample(True, conn=c, out_dir=tmp_path)
    ups = [q for q in c.log if q.lstrip().startswith("UPDATE")]
    assert "is_pilot = false" in ups[0] and any("is_pilot = true" in q for q in ups[1:])
    assert c.commits == 1 and "pilot_max_per_hotel" in msg
    df = pd.read_csv(tmp_path / "pilot_100.csv")
    assert list(df.columns) == EXPORT_COLS and len(df) == 2 * min(10, load_config()["pilot_max_per_hotel"])


def test_export_columns_exact():
    assert EXPORT_COLS == ["review_id", "hotel_id", "hotel_name", "destination", "final_star_rating", "published_date",
                           "language", "rating", "trip_info_trip_type", "subrating_value", "subrating_rooms",
                           "subrating_location", "subrating_cleanliness", "subrating_service",
                           "subrating_sleep_quality", "title", "text"]


def test_force_refused_with_limit_or_destination(tmp_path):
    for kw in ({"limit": 1}, {"destination": "Bali"}):
        c = FakeConn(True, db_rows())
        with pytest.raises(RuntimeError, match="--force"):
            pilot_sample(True, conn=c, out_dir=tmp_path, **kw)
        assert c.log == [] and c.commits == 0


def test_source_query_requires_complete_hotels(tmp_path):
    c = FakeConn(False, db_rows())
    pilot_sample(False, conn=c, out_dir=tmp_path)
    assert any("scrape_status = 'complete'" in q and "in_final_sample" in q for q in c.log)


def frame(spec):
    """spec: list of (dest, star, [(hotel_id, n_reviews, n_id)...])"""
    rows, rid = [], 1
    for d, st, hotels in spec:
        for h, n, nid in hotels:
            for i in range(n):
                rows.append({"review_id": rid, "hotel_id": h, "destination": d, "final_star_rating": st,
                             "language": "id" if i < nid else "en"})
                rid += 1
    return pd.DataFrame(rows)


def test_capacity_zero_stratum_and_pooled_shortfall_mixed_hotels():
    spec = [("Bali", 3, [(1, 50, 0), (2, 1, 0), (3, 0, 0)]), ("Bali", 4, [(4, 50, 0), (5, 50, 0), (6, 2, 0)]),
            ("Jakarta", 5, [(7, 2, 0)])]
    df = frame(spec)  # capacities with cap 4: 5, 10, 2 -> 17 < 100
    out = draw_pilot(df, 42, 4, 20, 100)
    assert len(out) == 17 and out.groupby("hotel_id").size().max() <= 4 and out.review_id.is_unique
    assert set(out.pilot_stratum) == {"Bali|3", "Bali|4", "Jakarta|5"}


def test_indonesian_concentrated_in_one_stratum():
    spec = [(d, s, [(i * 10 + k, 30, 0) for k in range(3)]) for i, (d, s) in enumerate(
        [(d, s) for d in ("Bali", "Jakarta", "Surabaya") for s in (3, 4, 5)])]
    spec[0] = ("Bali", 3, [(k, 30, 30) for k in range(3)])  # only Bali|3 has Indonesian, capacity 12 < 20 wanted
    out = draw_pilot(frame(spec), 42, 4, 20, 100)
    assert (out.language == "id").sum() == 11 and len(out) == 100  # Bali|3 quota is 11-12; all of it Indonesian


def test_summary_flags_shortfalls_27_hotel_like():
    spec = [(d, s, [(100 * i + k, 6, 1) for k in range(3)]) for i, (d, s) in enumerate(
        [(d, s) for d in ("Bali", "Jakarta", "Surabaya") for s in (3, 4, 5)])]
    df = frame(spec)
    out = draw_pilot(df, 42, 4, 20, 100)  # capacity 12/stratum -> 100 fits, but only 27 Indonesian exist
    cfg = {"pilot_max_per_hotel": 4, "pilot_size": 100, "pilot_min_id": 40}
    msg = summarise(out, df, cfg)
    assert "deviation from the registered 2" in msg and "SHORT of Indonesian minimum" in msg and "capacity is the reason" in msg
    small = df[df.hotel_id % 100 == 0]
    out2 = draw_pilot(small, 42, 4, 20, 100)
    assert "SHORT of target (capacity): got" in summarise(out2, small, cfg | {"pilot_min_id": 1})
