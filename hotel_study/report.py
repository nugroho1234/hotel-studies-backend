import math

import pandas as pd

from hotel_study.cli import same_destination
from hotel_study.config import ROOT, load_config
from hotel_study.db import connect

# Counts only: the registration says the eligibility test examines counts, never review content or ratings.
SQL = {
    "hotels": "SELECT hotel_id, destination, final_star_rating, n_raw_reviews, n_out_of_window, n_other_language, "
              "n_eligible_reviews, is_eligible FROM hotels WHERE in_eligibility_test ORDER BY hotel_id",
    "pages": "SELECT r.hotel_id, MAX(r.input_page_number) AS pages FROM reviews r "
             "JOIN hotels h USING (hotel_id) WHERE h.in_eligibility_test GROUP BY r.hotel_id",
    "prescreened": "SELECT destination, final_star_rating, COUNT(*) AS prescreened FROM hotels "
                   "WHERE cell_order IS NOT NULL GROUP BY destination, final_star_rating",
    "spend": "SELECT pass, COUNT(*) AS runs, COALESCE(SUM(usage_usd), 0) AS usd FROM scrape_runs GROUP BY pass",
}


def suggest_end_page(reviews_per_page: float, target: int) -> int:
    return math.ceil(target / reviews_per_page)


def _row(g: pd.DataFrame, pre: float, ceiling: int | None) -> dict:
    n, k = len(g), int(g["is_eligible"].fillna(False).astype(bool).sum())
    share = k / n if n else math.nan  # no test hotels: share and projection unknown (nan), never 0
    proj = pre * share
    return {"n_test_hotels": n, "n_eligible_hotels": k, "share_eligible": share, "prescreened": pre,
            "projected_eligible": proj, "below_ceiling": bool(ceiling is not None and proj < ceiling)}


def eligibility_summary(df: pd.DataFrame, prescreened: pd.DataFrame, ceiling: int | None = None) -> pd.DataFrame:
    """Per destination x star cell, pooled per star class (destination 'ALL') and one total row.
    projected_eligible = prescreened x share_eligible, flagged below_ceiling when under the config ceiling."""
    key = ["destination", "final_star_rating"]
    df, pre = df.copy(), prescreened.copy()
    for f in (df, pre):
        f["final_star_rating"] = f["final_star_rating"].astype(int).astype(str)
    cells = pd.concat([df[key], pre[key]]).drop_duplicates().sort_values(key)
    pre_n = {(r.destination, r.final_star_rating): r.prescreened for r in pre.itertuples()}
    rows = []
    for d, s in cells.itertuples(index=False):
        rows.append({"destination": d, "final_star_rating": s,
                     **_row(df[(df.destination == d) & (df.final_star_rating == s)], pre_n.get((d, s), 0), ceiling)})
    for s in sorted(cells.final_star_rating.unique()):
        rows.append({"destination": "ALL", "final_star_rating": s,
                     **_row(df[df.final_star_rating == s], pre[pre.final_star_rating == s].prescreened.sum(), ceiling)})
    rows.append({"destination": "ALL", "final_star_rating": "ALL", **_row(df, pre.prescreened.sum(), None)})
    return pd.DataFrame(rows)


def eligibility_metrics(df: pd.DataFrame, min_pages: int = 10) -> dict:
    """reviews_per_page: pooled sum(raw)/sum(pages) over hotels with pages >= min_pages (small hotels end on a
    partial page and bias a per-hotel median low). Shares are pooled over all test hotels."""
    big = df[df["pages"].fillna(0) >= min_pages]
    rate = big.n_raw_reviews.sum() / big.pages.sum() if len(big) else math.nan
    raw, out, el = df.n_raw_reviews.sum(), df.n_out_of_window.sum(), df.n_eligible_reviews.sum()
    return {"reviews_per_page": rate, "n_rate_hotels": len(big),
            "rate_max": (big.n_raw_reviews / big.pages).max() if len(big) else math.nan,
            "rate_median": (big.n_raw_reviews / big.pages).median() if len(big) else math.nan,
            "in_window_share": (raw - out) / raw if raw else math.nan,
            "en_id_share": el / (raw - out) if raw - out else math.nan}


def report_eligibility_test(limit: int | None = None, destination: str | None = None, conn=None, out_dir=None) -> str:
    cfg = load_config()
    for k in ("ceiling", "rate_min_pages", "target_raw_reviews_per_hotel", "max_spend_usd"):
        if cfg.get(k) is None:
            raise RuntimeError(f"{k} is not set in config/study.yaml")
    own = conn is None
    conn = conn or connect()
    try:
        def q(k):
            cur = conn.execute(SQL[k])
            return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
        hotels, pages, pre, spend = q("hotels"), q("pages"), q("prescreened"), q("spend")
    finally:
        if own:
            conn.close()
    hotels = hotels[hotels.n_raw_reviews.notna()]  # not yet scraped: not a result
    n_scraped = len(hotels)  # spend covers all of them, whatever --destination/--limit say
    hotels = hotels[hotels.destination.map(lambda d: same_destination(d, destination))]
    pre = pre[pre.destination.map(lambda d: same_destination(d, destination))]
    if limit:
        hotels = hotels.head(limit)
    df = hotels.merge(pages, on="hotel_id", how="left")
    if df.empty:
        return "No eligibility-test hotels with results yet."
    summary = eligibility_summary(df, pre, cfg["ceiling"])
    m = eligibility_metrics(df, cfg["rate_min_pages"])
    out = (out_dir or ROOT / "output") / "eligibility_report.csv"
    out.parent.mkdir(exist_ok=True)
    summary.to_csv(out, index=False)
    test = spend[spend["pass"] == "eligibility_test"]
    usd, runs = float(test.usd.sum()), int(test.runs.sum())
    lines = [summary.round(3).to_string(index=False), "",
             f"reviews per page (pooled, hotels with >= {cfg['rate_min_pages']} pages): {m['reviews_per_page']:.1f} "
             f"({m['n_rate_hotels']} hotels; per-hotel max {m['rate_max']:.1f}, median {m['rate_median']:.1f})",
             f"in-window share of raw reviews: {m['in_window_share']:.3f}",
             f"EN/ID share of in-window reviews: {m['en_id_share']:.3f}",
             f"eligibility-test spend: ${usd:.2f} over {runs} runs, ${usd / n_scraped:.2f} per scraped test hotel ({n_scraped} hotels, unfiltered)",
             f"total spend all passes: ${float(spend.usd.sum()):.2f} of max_spend_usd {cfg['max_spend_usd']}"]
    if m["n_rate_hotels"] < 3:
        lines.append(f"WARNING: only {m['n_rate_hotels']} hotels qualify for the rate (need 3); estimate unreliable, do not set end_page from it")
    if not math.isnan(m["reviews_per_page"]):
        lines.append(f"SUGGESTION only (set end_page in config by hand): "
                     f"{suggest_end_page(m['reviews_per_page'], cfg['target_raw_reviews_per_hotel'])} "
                     f"for ~{cfg['target_raw_reviews_per_hotel']} raw reviews per hotel")
    return "\n".join(lines) + f"\n\nWrote {out}"
