import logging
from collections import defaultdict

from hotel_study import db
from hotel_study.config import get_secret
from hotel_study.privacy import flatten_review, in_window

log = logging.getLogger(__name__)


def split_items(items: list[dict], cfg: dict, salt: str) -> tuple[list[dict], dict]:
    """Pure: flatten, drop out-of-window, set lang_ok, count per hotel."""
    w, langs = cfg["review_window"], cfg["languages"]
    rows, counts = [], defaultdict(lambda: dict.fromkeys(("n_raw", "n_out_of_window", "n_other_language", "n_eligible"), 0))
    for it in items:
        r = flatten_review(it, salt)  # raw item is never kept or written
        c = counts[r["hotel_id"]]
        c["n_raw"] += 1
        if not in_window(str(it.get("publishedDate")), w["start"], w["end"]):
            c["n_out_of_window"] += 1
            continue
        if r["translation_type"] is not None:
            log.error("review %s has translation_type=%r but no translation was requested", r["review_id"], r["translation_type"])
        r["lang_ok"] = r["language"] in langs and r["translation_type"] is None
        c["n_eligible" if r["lang_ok"] else "n_other_language"] += 1
        rows.append(r)
    return rows, dict(counts)


def ingest_items(conn, run_id: str, items: list[dict], cfg: dict) -> dict:
    """Upsert in-window reviews (idempotent on review_id); returns {hotel_id: counts}. Caller commits."""
    rows, counts = split_items(items, cfg, get_secret("USER_HASH_SALT"))
    for r in rows:
        r["scrape_run_id"] = run_id
    db.upsert(conn, "reviews", rows, "review_id")
    return counts
