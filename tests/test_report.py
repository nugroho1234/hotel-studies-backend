import math
import re

import pandas as pd

from hotel_study import report
from hotel_study.report import eligibility_metrics, eligibility_summary, suggest_end_page


def _df(rows):
    return pd.DataFrame(rows, columns=["destination", "final_star_rating", "n_raw_reviews", "n_out_of_window",
                                       "n_other_language", "n_eligible_reviews", "is_eligible", "pages"])


def _pre(rows):
    return pd.DataFrame(rows, columns=["destination", "final_star_rating", "prescreened"])


DF = _df([("Bali", 4, 600, 100, 100, 400, True, 40), ("Bali", 4, 300, 50, 160, 90, False, 20),
          ("Bali", 4, 500, 0, 50, 450, True, None)])


def test_share_and_projection():
    s = eligibility_summary(DF, _pre([("Bali", 4, 30), ("Bali", 5, 10)]), ceiling=30)
    r = s[(s.destination == "Bali") & (s.final_star_rating == "4")].iloc[0]
    assert r.n_test_hotels == 3 and r.n_eligible_hotels == 2
    assert math.isclose(r.share_eligible, 2 / 3) and math.isclose(r.projected_eligible, 30 * 2 / 3)
    assert r.below_ceiling  # 20 < 30


def test_cell_without_test_hotels_does_not_crash():
    s = eligibility_summary(DF, _pre([("Bali", 4, 30), ("Bali", 5, 10)]), ceiling=30)
    r = s[(s.destination == "Bali") & (s.final_star_rating == "5")].iloc[0]
    assert r.n_test_hotels == 0 and r.prescreened == 10
    assert math.isnan(r.share_eligible) and math.isnan(r.projected_eligible)  # unknown, not 0
    assert not r.below_ceiling


def test_pooled_by_star_and_total():
    df = pd.concat([DF, _df([("Lombok", 4, 300, 0, 0, 100, True, 20)])])
    s = eligibility_summary(df, _pre([("Bali", 4, 30), ("Lombok", 4, 10)]), ceiling=30)
    p = s[(s.destination == "ALL") & (s.final_star_rating == "4")].iloc[0]
    assert p.n_test_hotels == 4 and p.n_eligible_hotels == 3 and p.prescreened == 40
    assert math.isclose(p.projected_eligible, 40 * 3 / 4)
    t = s[(s.destination == "ALL") & (s.final_star_rating == "ALL")].iloc[0]
    assert t.n_test_hotels == 4


def test_metrics_median_skips_null_and_zero_pages():
    df = _df([("A", 3, 400, 0, 0, 1, True, 20), ("A", 3, 600, 0, 0, 1, True, 30),
              ("A", 3, 100, 0, 0, 1, True, None), ("A", 3, 100, 0, 0, 1, True, 0),
              ("A", 3, 90, 0, 0, 1, True, 10)])
    assert eligibility_metrics(df)["reviews_per_page"] == 20  # median of 20, 20, 9


def test_in_window_and_en_id_share():
    m = eligibility_metrics(DF)  # raw 1400, out 150, eligible 940
    assert math.isclose(m["in_window_share"], 1250 / 1400)
    assert math.isclose(m["en_id_share"], 940 / 1250)


def test_suggest_end_page():
    assert suggest_end_page(15, 600) == 40 and suggest_end_page(14, 600) == 43


def test_sql_reads_counts_only():
    for q in report.SQL.values():
        low = q.lower()
        for banned in (r"\btext\b", r"\btitle\b", r"\brating\b", "user_id_hash", "owner_response", "hometown"):
            assert not re.search(banned, low), (banned, q)
        assert "select *" not in low
