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


FCOLS = ["hotel_id", "destination", "star_class", "num_reviews_ge_100", "star_check_status", "in_sample_frame",
         "final_star_rating", "scrape_status", "is_eligible", "in_final_sample", "n_raw_reviews", "n_out_of_window",
         "n_other_language", "n_eligible_reviews"]
# 6 hotels: 1 final sample; 2 scraped, ineligible; 3 scraped (needs_pass2); 4 not_needed; 5 half-star agree, <100 reviews
# excluded earlier; 6 disagree (final NULL)
FH = pd.DataFrame([
    (1, "Bali", 4, True, "agree", True, 4, "complete", True, True, 600, 50, 50, 500),
    (2, "Bali", 4, True, "agree_half_star", True, 4, "complete", False, False, 300, 10, 200, 90),
    (3, "Bali", 4, True, "google_missing", True, 4, "needs_pass2", None, False, 100, 0, 0, 100),
    (4, "Bali", 4, True, "agree", True, 4, "not_needed", None, False, None, None, None, None),
    (5, "Bali", 4, False, None, False, None, "not_scraped", None, False, None, None, None, None),
    (6, "Bali", 4, True, "disagree", False, None, "not_scraped", None, False, None, None, None, None)], columns=FCOLS)


def test_funnel_stage_counts_and_monotone():
    f = report.funnel(FH, {1: 300})
    r = f[(f.destination == "Bali") & (f.star_class == "4")].iloc[0]
    hotel_cols = [c for c in f.columns if c.startswith(("1_", "2_", "3_", "4_", "5_", "6_"))]
    assert [int(r[c]) for c in hotel_cols] == [6, 5, 4, 3, 1, 1]  # listed, >=100, agree, scraped, eligible, final
    assert all(a >= b for a, b in zip([r[c] for c in hotel_cols], [r[c] for c in hotel_cols][1:]))
    assert r["7_reviews_selected (final_star_rating)"] == 300
    assert r["status_disagree (star_class)"] == 1 and r["status_agree (star_class)"] == 2
    t = f[(f.destination == "ALL") & (f.star_class == "ALL")].iloc[0]
    assert t["1_hotels_listed (star_class)"] == 6


def test_funnel_empty_cell_and_empty_frame_do_not_crash():
    f = report.funnel(FH[FH.hotel_id == 6], {})
    assert f["3_star_classes_agree (final_star_rating)"].sum() == 0
    assert not report.funnel(FH.iloc[0:0], {}).empty  # still the ALL row, zeros


def test_funnel_selected_maps_by_hotel_across_cells():
    h = FH.copy()
    h.loc[h.hotel_id == 2, ["destination", "final_star_rating"]] = ["Lombok", 5]
    f = report.funnel(h, pd.Series({1: 300, 2: 120}))
    sel = "7_reviews_selected (final_star_rating)"
    assert f[(f.destination == "Bali") & (f.star_class == "4")][sel].iloc[0] == 300
    assert f[(f.destination == "Lombok") & (f.star_class == "5")][sel].iloc[0] == 120
    assert f[f.destination == "ALL"][sel].iloc[-1] == 420


def test_acceptance_checks_flag_violations():
    h = FH.copy()
    extra = pd.DataFrame([dict(FH.iloc[0], hotel_id=100 + i) for i in range(30)])  # cell now has 31 final hotels
    h = pd.concat([h, extra])
    checks = dict((n, ok) for n, ok, _ in report.acceptance_checks(h, {1: 301}, {1: 301, 4: 5}, ceiling=30, max_selected=300))
    assert not checks["cell ceiling"] and not checks["selected per hotel"] and not checks["not_needed without reviews"]
    ok = report.acceptance_checks(FH, {1: 300}, {1: 300}, ceiling=30, max_selected=300)
    assert all(o for _, o, _ in ok)


def test_per_hotel_counts_final_sample_only(tmp_path):
    p = report.write_review_counts(FH, {1: 300}, tmp_path)
    c = pd.read_csv(p)
    assert list(c.columns) == ["hotel_id", "destination", "final_star_rating", "n_raw_reviews", "n_out_of_window",
                               "n_other_language", "n_eligible_reviews", "n_selected"]
    assert c.hotel_id.tolist() == [1] and c.n_selected.tolist() == [300]


class FunnelConn:
    def execute(self, q):
        if q == report.SQL["funnel_hotels"]:
            return _Cur(FCOLS, [tuple(r) for r in FH.astype(object).where(FH.notna(), None).itertuples(index=False)])
        assert q == report.SQL["review_counts"]
        return _Cur(["hotel_id", "n_reviews", "n_selected"], [(1, 700, 300)])


def test_report_funnel_end_to_end(tmp_path, monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setitem(report.load_config(), "reviews_per_hotel", 300)
    out = report.report_funnel(conn=FunnelConn(), out_dir=tmp_path)
    assert "PASS" in out and "FAIL" not in out and "star_basis" in out
    assert (tmp_path / "funnel.csv").exists() and (tmp_path / "review_counts_by_hotel.csv").exists()



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


def test_plain_report_runs_funnel(monkeypatch):
    from typer.testing import CliRunner
    from hotel_study.cli import app
    monkeypatch.setattr(report, "report_funnel", lambda limit=None, destination=None: "FUNNEL OK")
    r = CliRunner().invoke(app, ["report"])
    assert r.exit_code == 0 and "FUNNEL OK" in r.output


def test_report_eligibility_flag_still_dispatches(monkeypatch):
    from typer.testing import CliRunner
    from hotel_study.cli import app
    monkeypatch.setattr(report, "report_eligibility_test", lambda limit=None, destination=None: "ELIG OK")
    r = CliRunner().invoke(app, ["report", "--eligibility-test"])
    assert r.exit_code == 0 and "ELIG OK" in r.output


def test_report_never_modifies_config(tmp_path, monkeypatch):
    from hotel_study.config import ROOT
    p = ROOT / "config" / "study.yaml"
    before = p.read_bytes()
    _cfg(monkeypatch)
    report.report_eligibility_test(conn=FakeConn(), out_dir=tmp_path)
    assert p.read_bytes() == before
