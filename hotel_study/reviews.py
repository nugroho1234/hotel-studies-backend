import logging
from collections import defaultdict
from datetime import datetime, timezone
from itertools import groupby
from urllib.parse import urldefrag

from apify_client import ApifyClient

from hotel_study import db
from hotel_study.db import upsert
from hotel_study.cli import same_destination
from hotel_study.config import get_secret, load_config, require_main_pass_ready
from hotel_study.google import _wait_for_usage, check_spend
from hotel_study.privacy import flatten_review, in_window

log = logging.getLogger(__name__)


def split_items(items: list[dict], cfg: dict, salt: str) -> tuple[list[dict], dict]:
    """Pure: flatten, drop out-of-window, set lang_ok, count per hotel."""
    w, langs = cfg["review_window"], cfg["languages"]
    rows, counts = [], defaultdict(lambda: dict.fromkeys(("n_raw", "n_out_of_window", "n_other_language", "n_eligible", "n_bad_date"), 0))
    for it in items:
        r = flatten_review(it, salt)  # raw item is never kept or written
        if r["review_id"] is None or r["hotel_id"] is None:
            log.error("skipping item without review_id/hotel_id (review id: %s)", r["review_id"])
            continue
        c = counts[r["hotel_id"]]
        c["n_raw"] += 1
        if r["published_date"] is None:
            log.error("review %s has a missing or unparsable publishedDate", r["review_id"])
            c["n_bad_date"] += 1
            continue
        if not in_window(r["published_date"].isoformat(), w["start"], w["end"]):
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


# --- scrape-reviews run loop -------------------------------------------------------------
# Actor epctex/tripadvisor-reviews-scraper, schema read from the public Apify API (build "default").
# Input:  startUrls (array of URL strings), since ("YYYY-MM-DD"), endPage (int, per start URL, null = no cap),
#         plus languages / ratings / maxItems / locale which we never send.
# Output: id, publishedDate, language, originalLanguage, translationType (null unless `languages` was set),
#         location.locationId (== hotels.hotel_id), inputSource.pageNumber (1-based page that produced the item),
#         additionalRatings = [{ratingLabel, rating}] (matches privacy.flatten_review).
# Note: the README example also has a top-level locationId and 2026 dates; we only rely on the fields above.
ACTOR = "epctex/tripadvisor-reviews-scraper"


def hotel_status(n_eligible: int, pages_returned: int, end_page: int | None, target: int = 300, threshold: int = 100) -> tuple[str, bool | None]:
    """(scrape_status, is_eligible) per PRD section 8. No end_page (eligibility test): everything since 2023 was fetched."""
    if end_page is None:
        return "complete", n_eligible >= threshold
    if n_eligible >= target:
        return "complete", True
    if pages_returned < end_page:
        return "complete", n_eligible >= threshold
    return "needs_pass2", True if n_eligible >= threshold else None


def final_sample(status: str, is_eligible: bool | None) -> bool:
    """Eligibility-test hotels have cell_order <= ceiling, so a complete, eligible one is always in the final sample."""
    return status == "complete" and is_eligible is True


def strip_fragment(url: str) -> str:
    return urldefrag(url)[0]  # review_url may carry '#/chat/...'


def build_run_input(urls: list[str], cfg: dict, end_page: int | None) -> dict:
    if len(urls) > cfg["batch_size"]:
        raise ValueError(f"{len(urls)} urls exceed batch_size {cfg['batch_size']}")
    i = {"startUrls": urls, "since": cfg["review_window"]["start"]}  # never languages / maxItems / ratings
    if end_page is not None:
        i["endPage"] = end_page
    return i


def make_batches(hotels: list[tuple], size: int) -> list[list[tuple]]:
    """hotels = [(hotel_id, destination, star, url)] sorted by cell; batches never cross a cell."""
    return [b for _, g in groupby(hotels, key=lambda h: (h[1], h[2])) for g in [list(g)] for b in (g[i:i + size] for i in range(0, len(g), size))]


def _pages(items: list[dict]) -> dict[int, int]:
    out: dict[int, int] = {}
    for it in items:
        h, p = (it.get("location") or {}).get("locationId"), (it.get("inputSource") or {}).get("pageNumber")
        if h is not None and p is not None:
            out[int(h)] = max(out.get(int(h), 0), p)
    return out


def _attempt(client, conn, batch, cfg, pass_, end_page) -> bool:
    check_spend(conn, cfg)
    run_input = build_run_input([strip_fragment(h[3]) for h in batch], cfg, end_page)
    run = client.actor(ACTOR).start(run_input=run_input)
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "pass": pass_, "input": run_input,  # provenance before ingest
                                  "dataset_id": run.default_dataset_id, "started_at": datetime.now(timezone.utc)}], "run_id")
    conn.commit()
    run = client.run(run.id).wait_for_finish()
    ok = str(getattr(run.status, "value", run.status)) == "SUCCEEDED"
    items = list(client.dataset(run.default_dataset_id).iterate_items()) if ok else []
    if ok:
        counts = {int(h): c for h, c in ingest_items(conn, run.id, items, cfg).items()}
        pages = _pages(items)
        for h, *_ in batch:
            c = counts.get(h) or dict.fromkeys(("n_raw", "n_out_of_window", "n_other_language", "n_eligible", "n_bad_date"), 0)
            if c["n_bad_date"]:
                log.warning("hotel %s: %d reviews with a missing/unparsable date were skipped", h, c["n_bad_date"])
            status, elig = hotel_status(c["n_eligible"], pages.get(h, 0), end_page, cfg["reviews_per_hotel"], cfg["eligibility"]["threshold"])
            conn.execute("UPDATE hotels SET n_raw_reviews=%s, n_out_of_window=%s, n_other_language=%s, n_eligible_reviews=%s, "
                         "is_eligible=%s, scrape_status=%s, in_final_sample=%s WHERE hotel_id=%s",
                         (c["n_raw"], c["n_out_of_window"], c["n_other_language"], c["n_eligible"], elig, status,
                          final_sample(status, elig) if end_page is None else False, h))
        conn.commit()
    g = cfg["google"]
    run, usage = _wait_for_usage(client, run, g["usage_max_wait_seconds"], g["usage_poll_seconds"])
    row = {"run_id": run.id, "actor": ACTOR, "finished_at": datetime.now(timezone.utc), "item_count": len(items)}
    if usage is not None and (usage > 0 or not items):
        row["usage_usd"] = usage  # left NULL if unconfirmed: check_spend then refuses further runs
    upsert(conn, "scrape_runs", [row], "run_id")
    conn.commit()
    if ok and cfg["delete_remote_datasets"]:
        client.dataset(run.default_dataset_id).delete()
    if not ok:
        log.error("Apify run %s ended with status %s", run.id, run.status)
    return ok


def run_batch(client, conn, batch, cfg, pass_, end_page) -> bool:
    """One actor run, retried once. False (hotels stay not_scraped) if both attempts fail; spend-cap errors propagate."""
    for attempt in (1, 2):
        try:
            if _attempt(client, conn, batch, cfg, pass_, end_page):
                return True
        except RuntimeError as e:
            if "max_spend_usd" in str(e) or "usage_usd" in str(e):
                raise
            log.error("run failed (attempt %d): %s", attempt, e)
    log.error("hotels %s failed twice; left not_scraped", [h[0] for h in batch])
    return False


def scrape_reviews(pass_: str, eligibility_test: bool, limit: int | None = None, destination: str | None = None, conn=None) -> str:
    cfg = load_config()
    if not eligibility_test:
        if pass_ == "1":
            require_main_pass_ready(cfg)
        raise NotImplementedError("main pass is Task 10")
    from hotel_study.db import connect
    own = conn is None
    conn = conn or connect()
    try:
        # resume: only hotels still not_scraped; eligibility-test hotels have cell_order <= eligibility_test_size
        rows = conn.execute("SELECT hotel_id, destination, final_star_rating, review_url FROM hotels "
                            "WHERE in_eligibility_test AND scrape_status = 'not_scraped' "
                            "ORDER BY destination, final_star_rating, cell_order").fetchall()
        hotels = [r for r in rows if same_destination(r[1] or "", destination)][:limit]
        client = ApifyClient(get_secret("APIFY_TOKEN"))
        done = failed = 0
        for b in make_batches(hotels, cfg["batch_size"]):
            if run_batch(client, conn, b, cfg, "eligibility_test", None):
                done += len(b)
            else:
                failed += len(b)
        return f"Eligibility test: {done} hotels scraped, {failed} failed."
    finally:
        if own:
            conn.close()
