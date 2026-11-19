from hotel_study.cli import same_destination
from hotel_study.config import load_config
from hotel_study.db import connect


def select_for_hotel(reviews: list[dict], n: int | None = None) -> list[dict]:
    """Pure: set lang_ok, is_selected, selection_rank (newest first, ties by review_id desc)."""
    cfg = load_config()
    n = cfg["reviews_per_hotel"] if n is None else n
    w = cfg["review_window"]
    out = [dict(r) for r in reviews]
    for r in out:
        r["lang_ok"] = r["language"] in cfg["languages"] and r["translation_type"] is None
        r["is_selected"], r["selection_rank"] = False, None
    # window filter is defensive: ingest already drops out-of-window reviews. ISO dates compare as strings.
    pool = [r for r in out if r["lang_ok"] and str(w["start"]) <= str(r["published_date"]) <= str(w["end"])]
    pool.sort(key=lambda r: (str(r["published_date"]), r["review_id"]), reverse=True)
    for rank, r in enumerate(pool[:n], 1):
        r["is_selected"], r["selection_rank"] = True, rank
    return out


def select_reviews(limit: int | None = None, destination: str | None = None, conn=None) -> str:
    """Recompute lang_ok / is_selected / selection_rank for final-sample complete hotels; one transaction."""
    cfg = load_config()
    own = conn is None
    conn = conn or connect()
    try:
        hotels = [h for h in conn.execute(
            "SELECT hotel_id, destination FROM hotels WHERE in_final_sample AND scrape_status = 'complete' "
            "ORDER BY hotel_id").fetchall() if same_destination(h[1] or "", destination)][:limit]
        # Hotels outside the final sample must never carry a selection.
        conn.execute("UPDATE reviews SET is_selected = false, selection_rank = NULL "
                     "WHERE hotel_id IN (SELECT hotel_id FROM hotels WHERE NOT in_final_sample) "
                     "AND (is_selected OR selection_rank IS NOT NULL)")
        for hid, _ in hotels:
            rows = conn.execute("SELECT review_id, published_date, language, translation_type FROM reviews "
                                "WHERE hotel_id = %s", (hid,)).fetchall()
            res = select_for_hotel([dict(zip(("review_id", "published_date", "language", "translation_type"), r))
                                    for r in rows], cfg["reviews_per_hotel"])
            # NULL first so reassigning never trips reviews_selection_rank_uq mid-transaction.
            conn.execute("UPDATE reviews SET selection_rank = NULL WHERE hotel_id = %s", (hid,))
            for r in res:
                conn.execute("UPDATE reviews SET lang_ok = %s, is_selected = %s, selection_rank = %s WHERE review_id = %s",
                             (r["lang_ok"], r["is_selected"], r["selection_rank"], r["review_id"]))
            conn.execute("UPDATE hotels SET n_eligible_reviews = %s WHERE hotel_id = %s",
                         (sum(r["lang_ok"] for r in res), hid))
        conn.commit()
        return f"Selected reviews for {len(hotels)} hotels."
    finally:
        if own:
            conn.close()
