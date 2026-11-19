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


def test_rate_pools_large_hotels_only():
    rows = [("A", 3, 30, 0, 0, 1, True, 3)] * 2 + [("A", 3, 590, 0, 0, 1, True, 40)] * 3
    m = eligibility_metrics(_df(rows), 10)
    assert math.isclose(m["reviews_per_page"], 590 / 40) and m["n_rate_hotels"] == 3
    assert math.isclose(m["rate_max"], 14.75) and math.isclose(m["rate_median"], 14.75)


def test_none_qualifying_is_nan():
    m = eligibility_metrics(_df([("A", 3, 30, 0, 0, 1, True, 3), ("A", 3, 9, 0, 0, 1, True, None)]), 10)
    assert math.isnan(m["reviews_per_page"]) and m["n_rate_hotels"] == 0


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


class _Cur:
    def __init__(self, cols, rows):
        self.description = [type("C", (), {"name": c}) for c in cols]
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self):
        h = [(1, "bali", 4, 600, 0, 0, 500, True), (2, "bali", 4, 600, 0, 0, 100, False),
             (3, "jakarta", 4, 600, 0, 0, 500, True), (4, "bali", 4, None, None, None, None, None)]
        self.data = {
            "hotels": _Cur(["hotel_id", "destination", "final_star_rating", "n_raw_reviews", "n_out_of_window",
                            "n_other_language", "n_eligible_reviews", "is_eligible"], h),
            "pages": _Cur(["hotel_id", "pages"], [(1, 40), (2, 40), (3, 40)]),
            "prescreened": _Cur(["destination", "final_star_rating", "prescreened"], [("bali", 4, 30), ("jakarta", 4, 20)]),
            "spend": _Cur(["pass", "runs", "usd"], [("eligibility_test", 3, 3.0), ("google", 1, 1.0)])}

    def execute(self, q):
        return self.data[next(k for k, v in report.SQL.items() if v == q)]


def _cfg(monkeypatch):
    cfg = {"ceiling": 30, "rate_min_pages": 10, "target_raw_reviews_per_hotel": 600, "max_spend_usd": 85, "end_page": None}
    monkeypatch.setattr(report, "load_config", lambda: cfg)


def test_report_end_to_end(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    out = report.report_eligibility_test(conn=FakeConn(), out_dir=tmp_path)
    assert "$4.00 of max_spend_usd 85" in out and "$1.00 per scraped test hotel (3 hotels" in out
    assert "SUGGESTION only" in out and " 40 for ~600" in out
    csv = pd.read_csv(tmp_path / "eligibility_report.csv")
    assert csv[(csv.destination == "ALL") & (csv.final_star_rating == "ALL")].n_test_hotels.iloc[0] == 3  # hotel 4 excluded


def test_report_destination_filter_keeps_spend_unfiltered(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    out = report.report_eligibility_test(destination="Bali", conn=FakeConn(), out_dir=tmp_path)
    csv = pd.read_csv(tmp_path / "eligibility_report.csv")
    assert set(csv.destination) == {"bali", "ALL"} and "$1.00 per scraped test hotel (3 hotels" in out
    assert "WARNING" in out  # only 2 hotels qualify


def test_report_missing_ceiling_errors(monkeypatch):
    monkeypatch.setattr(report, "load_config", lambda: {})
    import pytest
    with pytest.raises(RuntimeError, match="ceiling"):
        report.report_eligibility_test(conn=FakeConn())


def test_plain_report_not_implemented():
    import pytest
    from typer.testing import CliRunner
    from hotel_study.cli import app
    r = CliRunner().invoke(app, ["report"])
    assert isinstance(r.exception, NotImplementedError)


def test_report_never_modifies_config(tmp_path, monkeypatch):
    from hotel_study.config import ROOT
    p = ROOT / "config" / "study.yaml"
    before = p.read_bytes()
    _cfg(monkeypatch)
    report.report_eligibility_test(conn=FakeConn(), out_dir=tmp_path)
    assert p.read_bytes() == before
