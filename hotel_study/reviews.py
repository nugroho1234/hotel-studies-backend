import logging
from collections import defaultdict
from datetime import datetime, timezone
from itertools import groupby
from urllib.parse import urldefrag

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from hotel_study import db
from hotel_study.db import upsert
from hotel_study.cells import cell_done, next_batch_size
from hotel_study.cli import same_destination
from hotel_study.config import get_secret, load_config, require_main_pass_ready
from hotel_study.google import SpendLimitReached, _wait_for_usage, check_spend
from hotel_study.parsing import parse_hotel_id
from hotel_study.privacy import flatten_review, in_window

log = logging.getLogger(__name__)


def split_items(items: list[dict], cfg: dict, salt: str, hotel_ids=None) -> tuple[list[dict], dict]:
    """Pure: flatten, drop foreign hotels and duplicate ids, drop out-of-window, set lang_ok, count per hotel."""
    w, langs = cfg["review_window"], cfg["languages"]
    rows, counts = [], defaultdict(lambda: dict.fromkeys(("n_raw", "n_out_of_window", "n_other_language", "n_eligible", "n_bad_date"), 0))
    seen = set()
    for it in items:
        r = flatten_review(it, salt, cfg["subrating_labels"])  # raw item is never kept or written
        if r["review_id"] is None or r["hotel_id"] is None:
            log.error("skipping item without review_id/hotel_id (review id: %s)", r["review_id"])
            continue
        try:
            r["hotel_id"] = int(r["hotel_id"])  # the actor's locationId type is unconfirmed (int or str); DB ids are int
        except (TypeError, ValueError):
            log.error("skipping review %s: non-numeric location id %r", r["review_id"], r["hotel_id"])
            continue
        if hotel_ids is not None and r["hotel_id"] not in hotel_ids:
            log.error("dropping review %s: hotel %s is not in this batch (redirected listing?)", r["review_id"], r["hotel_id"])
            continue
        if r["review_id"] in seen:  # pages shift while scraping; first occurrence wins
            continue
        seen.add(r["review_id"])
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
        if r["rating"] is None:
            log.warning("review %s has no overall rating; not eligible (counted in n_other_language)", r["review_id"])
        r["lang_ok"] = r["language"] in langs and r["translation_type"] is None and r["rating"] is not None
        c["n_eligible" if r["lang_ok"] else "n_other_language"] += 1
        rows.append(r)
    return rows, dict(counts)


def ingest_items(conn, run_id: str, items: list[dict], cfg: dict, hotel_ids=None) -> dict:
    """Upsert in-window reviews (idempotent on review_id); returns {hotel_id: counts}. Caller commits."""
    rows, counts = split_items(items, cfg, get_secret("USER_HASH_SALT"), hotel_ids)
    for r in rows:
        r["scrape_run_id"] = run_id
    db.upsert(conn, "reviews", rows, "review_id")
    return counts


# --- scrape-reviews run loop -------------------------------------------------------------
# Actor epctex/tripadvisor-reviews-scraper, schema read from the public Apify API (build "default").
# Input:  startUrls (array of URL strings), since ("YYYY-MM-DD"), endPage (int, per start URL, null = no cap),
#         locale (TripAdvisor domain, e.g. "id"; always sent from config review_locale).
#         languages / ratings / maxItems are never sent. Experiment on one hotel, 3 pages: no locale ->
#         {en/en:3, id/id:3, en/id MACHINE:39}; locale="id" -> {en/en:3, id/id:42}, no MACHINE rows.
#         `languages` stays unset (it is what produces machine translations).
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


def build_run_input(urls: list[str], cfg: dict, end_page: int | None, eligibility_test: bool = False) -> dict:
    if len(urls) > cfg["batch_size"]:
        raise ValueError(f"{len(urls)} urls exceed batch_size {cfg['batch_size']}")
    if end_page is None and not eligibility_test and len(urls) > cfg["no_endpage_batch_size"]:
        raise ValueError(f"{len(urls)} urls exceed no_endpage_batch_size {cfg['no_endpage_batch_size']} for a run without endPage")
    if not cfg.get("review_locale"):
        raise ValueError("review_locale is not set in config/study.yaml; without it Indonesian reviews return as machine translations")
    i = {"startUrls": urls, "since": cfg["review_window"]["start"], "locale": cfg["review_locale"]}  # never languages / maxItems / ratings
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


def _update_hotels(conn, run_id, items, ids, cfg, end_page) -> int:
    """Ingest items, then SET each hotel's counts and status. Returns hotels updated. Hotels with no raw reviews stay not_scraped."""
    counts = {int(h): c for h, c in ingest_items(conn, run_id, items, cfg, set(ids)).items()}
    pages, n = _pages(items), 0
    for h in ids:
        c = counts.get(h)
        if not c or c["n_raw"] == 0:
            log.error("hotel %s: run returned no reviews; left not_scraped for a rerun", h)
            continue
        if c["n_bad_date"]:
            log.warning("hotel %s: %d reviews with a missing/unparsable date were skipped", h, c["n_bad_date"])
        if end_page is None:  # a no-endPage re-scrape must not replace a bigger stored result with a smaller, possibly truncated one
            row = conn.execute("SELECT n_raw_reviews FROM hotels WHERE hotel_id=%s", (h,)).fetchone()
            if row and row[0] is not None and c["n_raw"] < row[0]:
                log.error("hotel %s: no-endPage run returned %d raw reviews, fewer than the stored %d; kept the stored row", h, c["n_raw"], row[0])
                continue
        status, elig = hotel_status(c["n_eligible"], pages.get(h, end_page or 0), end_page, cfg["reviews_per_hotel"], cfg["eligibility"]["threshold"])
        conn.execute("UPDATE hotels SET n_raw_reviews=%s, n_out_of_window=%s, n_other_language=%s, n_eligible_reviews=%s, "
                     "is_eligible=%s, scrape_status=%s, in_final_sample = CASE WHEN in_eligibility_test THEN %s ELSE in_final_sample END "
                     "WHERE hotel_id=%s",
                     (c["n_raw"], c["n_out_of_window"], c["n_other_language"], c["n_eligible"], elig, status,
                      final_sample(status, elig), h))  # only eligibility-test hotels get it here; scrape_cell finalises the rest
        n += 1
    conn.commit()
    return n


def _finish(client, conn, run, n_items: int, cfg: dict, delete: bool, actor: str = ACTOR) -> None:
    """Record finished_at / item_count / usage_usd, then delete the remote dataset per config (privacy)."""
    g = cfg["google"]
    run, usage = _wait_for_usage(client, run, g["usage_max_wait_seconds"], g["usage_poll_seconds"])
    row = {"run_id": run.id, "actor": actor, "finished_at": datetime.now(timezone.utc), "item_count": n_items}
    if usage is not None and (usage > 0 or not n_items):
        row["usage_usd"] = usage  # left NULL if unconfirmed: check_spend then refuses further runs
    upsert(conn, "scrape_runs", [row], "run_id")
    conn.commit()
    if delete and cfg["delete_remote_datasets"]:
        try:
            client.dataset(run.default_dataset_id).delete()
        except (ApifyApiError, OSError) as e:  # the batch is already ingested and recorded; never retry because of this
            log.error("could not delete remote dataset %s: %s", run.default_dataset_id, e)


def _succeeded(run) -> bool:
    return str(getattr(run.status, "value", run.status)) == "SUCCEEDED"


def _attempt(client, conn, batch, cfg, pass_, end_page) -> int | None:
    """One actor run. Returns hotels updated, or None if the run did not succeed. A crash mid-way leaves the row unfinished for reconcile()."""
    check_spend(conn, cfg)
    run_input = build_run_input([strip_fragment(h[3]) for h in batch], cfg, end_page, eligibility_test=pass_ == "eligibility_test")
    run = client.actor(ACTOR).start(run_input=run_input)
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "pass": pass_, "input": run_input,  # provenance before ingest
                                  "dataset_id": run.default_dataset_id, "started_at": datetime.now(timezone.utc)}], "run_id")
    conn.commit()
    done = client.run(run.id).wait_for_finish()
    if done is None:
        raise RuntimeError(f"Apify run {run.id}: wait_for_finish returned nothing; will be reconciled")
    if not _succeeded(done):
        log.error("Apify run %s ended with status %s", done.id, done.status)
        _finish(client, conn, done, 0, cfg, delete=True)
        return None
    items = list(client.dataset(done.default_dataset_id).iterate_items())
    n = _update_hotels(conn, done.id, items, [h[0] for h in batch], cfg, end_page)  # if this raises, dataset is kept for reconcile()
    _finish(client, conn, done, len(items), cfg, delete=True)
    return n


def run_batch(client, conn, batch, cfg, pass_, end_page) -> int | None:
    """One actor run, retried once on run or Apify/network errors. None (hotels stay not_scraped) if both fail.
    Spend-guard and DB errors propagate (a DB error leaves the run row unfinished for reconcile())."""
    for attempt in (1, 2):
        try:
            n = _attempt(client, conn, batch, cfg, pass_, end_page)
            if n is not None:
                return n
        except SpendLimitReached:
            raise
        except (RuntimeError, ApifyApiError, OSError) as e:
            log.error("run failed (attempt %d): %s", attempt, e)
    log.error("hotels %s failed twice; left not_scraped", [h[0] for h in batch])
    return None


def reconcile(client, conn, cfg: dict) -> int:
    """Settle scrape_runs rows left unfinished by a crash: record usage, ingest a succeeded run's stored dataset
    (never start a new run), delete datasets per config. Raises RuntimeError if anything cannot be settled."""
    rows = conn.execute("SELECT run_id, dataset_id, input, actor FROM scrape_runs WHERE finished_at IS NULL").fetchall()
    for run_id, dataset_id, inp, actor in rows:
        try:
            run = client.run(run_id).get()
            if run is None:
                raise RuntimeError("run not found")
            status = str(getattr(run.status, "value", run.status))
            if status in ("READY", "RUNNING"):
                raise RuntimeError(f"still {status}; wait for it to end")
            items, delete = [], True
            if status == "SUCCEEDED" and actor == ACTOR:  # other actors (google) are only closed out, never ingested here
                inp = inp or {}
                ids = [i for i in (parse_hotel_id(u) for u in inp.get("startUrls", [])) if i]
                todo = [r[0] for r in conn.execute("SELECT hotel_id FROM hotels WHERE hotel_id = ANY(%s) AND scrape_status IN ('not_scraped', 'needs_pass2')", (ids,)).fetchall()]
                if todo:
                    items = list(client.dataset(dataset_id).iterate_items())
                    _update_hotels(conn, run_id, items, todo, cfg, inp.get("endPage"))
            _finish(client, conn, run, len(items), cfg, delete, actor)
        except (RuntimeError, ApifyApiError, OSError) as e:
            raise RuntimeError(f"Could not reconcile unfinished run {run_id}: {e}. No new runs started. Manual way out: "
                               f"update scrape_runs set finished_at = now(), usage_usd = <value from the Apify console> where run_id = '{run_id}'. "
                               "If it SUCCEEDED but its dataset expired, its hotels stay not_scraped and will be re-scraped (costs money).") from e
    return len(rows)


def _spent(conn) -> float:
    return conn.execute("SELECT coalesce(sum(usage_usd), 0) FROM scrape_runs").fetchone()[0]


def _cell_rows(conn, cell):
    """[(hotel_id, url, scrape_status, is_eligible, in_eligibility_test)] in cell_order."""
    return conn.execute("SELECT hotel_id, review_url, scrape_status, is_eligible, in_eligibility_test FROM hotels "
                        "WHERE destination = %s AND final_star_rating = %s AND cell_order IS NOT NULL ORDER BY cell_order", cell).fetchall()


def _run(client, conn, cell, rows, pass_, end_page, cfg) -> None:
    """One batch (+ policy for hotels the run did not settle). RuntimeError if the cell cannot proceed."""
    def attempt(rs):
        n = run_batch(client, conn, [(r[0], cell[0], cell[1], r[1]) for r in rs], cfg, pass_, end_page)
        if n is None:
            raise RuntimeError(f"cell {cell}: run failed for hotels {[r[0] for r in rs]}; rerun scrape-reviews --pass 1 to retry")
        now = {r[0]: r for r in _cell_rows(conn, cell)}  # one query per attempt
        return n, [r[0] for r in rs if now[r[0]][2] == "not_scraped" or (pass_ == "2" and now[r[0]][2] == "needs_pass2" and now[r[0]][3] is None)]
    n1, left = attempt(rows)
    if left and pass_ == "2":  # pass 1 had ~end_page pages for these, so zero now is a scraper failure; keep their counts, one attempt per rerun
        raise RuntimeError(f"cell {cell}: undecided hotels {left} returned no usable result (no reviews, or fewer than stored) in the no-endPage re-scrape; check them manually, then rerun")
    if left:  # retry once; still nothing means genuinely no reviews since the window start
        _, left = attempt([r for r in rows if r[0] in left])
        if left and (n1 == 0 or len(rows) == 1):  # nothing else in the run returned anything: scraper failure vs real zero is ambiguous
            raise RuntimeError(f"cell {cell}: hotels {left} returned no reviews twice, but the run returned nothing for the rest of its batch "
                               "(whole-batch or single-hotel empty result), so it is ambiguous (scraper failure vs real zero). Check on TripAdvisor; "
                               "to mark one ineligible by hand: update hotels set scrape_status='complete', n_raw_reviews=0, n_out_of_window=0, "
                               "n_other_language=0, n_eligible_reviews=0, is_eligible=false, in_final_sample=false where hotel_id = " + str(left[0]) + (f" (repeat for each of {left})" if len(left) > 1 else ""))
        if left:
            log.error("hotels %s returned no reviews twice; recorded as complete, not eligible, 0 reviews", left)
            conn.execute("UPDATE hotels SET scrape_status = 'complete', n_raw_reviews = 0, n_out_of_window = 0, n_other_language = 0, "
                         "n_eligible_reviews = 0, is_eligible = false, in_final_sample = false WHERE hotel_id = ANY(%s)", (left,))
            conn.commit()
    print(f"cell {cell} pass {pass_}: hotels {[r[0] for r in rows]} done, spent ${_spent(conn):.2f}")


def scrape_cell(client, conn, cell: tuple, cfg: dict) -> int:
    """PRD section 8 main pass 1 for one cell. Returns the cell's eligible hotel count.
    Undecided hotels (needs_pass2, <threshold) are re-scraped without endPage first, even when the ceiling is already met,
    because a decided-eligible early hotel can displace a later one in the final sample."""
    ceiling, size = cfg["ceiling"], cfg["batch_size"]
    while True:
        rows = _cell_rows(conn, cell)
        n_elig = sum(r[3] is True for r in rows)
        # an undecided hotel ordered after the ceiling-th eligible one can never enter the final sample
        cut = [i for i, r in enumerate(rows) if r[3] is True][ceiling - 1] if n_elig >= ceiling else len(rows)
        undecided = [r for r in rows[:cut] if r[2] == "needs_pass2" and r[3] is None]
        if undecided:
            _run(client, conn, cell, undecided[:cfg["no_endpage_batch_size"]], "2", None, cfg)
            continue
        # not_scraped hotels ordered before the ceiling-th eligible one keep their place in the sample, so they are scraped first
        todo = [r for r in rows[:cut] if r[2] == "not_scraped"]
        if not todo:
            break
        tested = [r for r in rows if r[4] and r[2] != "not_scraped"]
        share = sum(r[3] is True for r in tested) / len(tested) if tested else 0.0  # eligibility-test eligible / scraped
        k = min(size, len(todo)) if cell_done(n_elig, ceiling) else next_batch_size(ceiling - n_elig, share, size, len(todo))  # needed > 0 and remaining > 0 here
        _run(client, conn, cell, todo[:k], "1", cfg["end_page"], cfg)
    conn.execute("UPDATE hotels SET scrape_status = 'not_needed' WHERE destination = %s AND final_star_rating = %s AND scrape_status = 'not_scraped'", cell)
    keep = [r[0] for r in rows if r[3] is True][:ceiling]
    conn.execute("UPDATE hotels SET in_final_sample = (hotel_id = ANY(%s)) WHERE destination = %s AND final_star_rating = %s", (keep, *cell))
    conn.commit()
    print(f"cell {cell} finished: {n_elig} eligible, {len(keep)} in final sample, spent ${_spent(conn):.2f}")
    return n_elig


def scrape_pass2(client, conn, cfg: dict, limit: int | None = None, destination: str | None = None) -> str:
    """Re-scrape final-sample hotels that hit endPage with >=threshold eligible reviews, with no endPage."""
    scope = [d for (d,) in conn.execute("SELECT DISTINCT destination FROM hotels WHERE cell_order IS NOT NULL").fetchall() if same_destination(d or "", destination)]
    if conn.execute("SELECT 1 FROM hotels h WHERE cell_order IS NOT NULL AND destination = ANY(%s) AND (scrape_status = 'not_scraped' OR "
                    "(scrape_status = 'needs_pass2' AND is_eligible IS NULL AND cell_order < coalesce((SELECT cell_order FROM ("
                    "SELECT cell_order, row_number() OVER (ORDER BY cell_order) rn FROM hotels h2 WHERE h2.destination = h.destination "
                    "AND h2.final_star_rating = h.final_star_rating AND h2.is_eligible) t WHERE rn = %s), 1000000))) LIMIT 1",
                    (scope, cfg["ceiling"])).fetchone():
        raise RuntimeError("pass 2 refused: some cells in scope still have not_scraped or undecided hotels; finish scrape-reviews --pass 1 first")
    rows = conn.execute("SELECT hotel_id, destination, final_star_rating, review_url FROM hotels "
                        "WHERE in_final_sample AND scrape_status = 'needs_pass2' ORDER BY destination, final_star_rating, cell_order").fetchall()
    hotels = [r for r in rows if same_destination(r[1] or "", destination)][:limit]
    done = failed = 0
    for b in make_batches(hotels, cfg["no_endpage_batch_size"]):
        n = run_batch(client, conn, b, cfg, "2", None)
        if n is None:
            failed += len(b)
        else:
            done += n
            failed += len(b) - n
            print(f"pass 2: hotels {[h[0] for h in b]} done, spent ${_spent(conn):.2f}")
    return f"Pass 2: {done} hotels scraped, {failed} failed."


def scrape_reviews(pass_: str, eligibility_test: bool, limit: int | None = None, destination: str | None = None, conn=None) -> str:
    cfg = load_config()
    if pass_ not in ("1", "2"):
        raise RuntimeError(f"--pass must be 1 or 2, got {pass_}")
    if not eligibility_test and pass_ == "1":
        require_main_pass_ready(cfg)
    from hotel_study.db import connect
    own = conn is None
    conn = conn or connect()
    try:
        client = ApifyClient(get_secret("APIFY_TOKEN"))
        reconcile(client, conn, cfg)
        if not eligibility_test and pass_ == "2":
            return scrape_pass2(client, conn, cfg, limit, destination)
        if not eligibility_test:  # --limit = at most N cells; completed cells are only finalised, never re-scraped
            cells = [tuple(c) for c in conn.execute("SELECT DISTINCT destination, final_star_rating FROM hotels WHERE cell_order IS NOT NULL "
                                                    "ORDER BY destination, final_star_rating").fetchall() if same_destination(c[0] or "", destination)][:limit]
            ok, bad = [], []
            for c in cells:
                try:
                    ok.append(f"{c[0]}/{c[1]}={scrape_cell(client, conn, c, cfg)}")
                except SpendLimitReached:
                    raise  # stops everything
                except RuntimeError as e:
                    bad.append(f"{c[0]}/{c[1]}: {e}")
            if bad:
                raise RuntimeError(f"Main pass 1: {len(ok)} cells finished ({', '.join(ok)}); {len(bad)} failed:\n" + "\n".join(bad))
            return f"Main pass 1: {len(cells)} cells, eligible per cell: " + ", ".join(ok)
        # resume: only hotels still not_scraped; eligibility-test hotels have cell_order <= eligibility_test_size
        rows = conn.execute("SELECT hotel_id, destination, final_star_rating, review_url FROM hotels "
                            "WHERE in_eligibility_test AND scrape_status = 'not_scraped' "
                            "ORDER BY destination, final_star_rating, cell_order").fetchall()
        hotels = [r for r in rows if same_destination(r[1] or "", destination)][:limit]
        done = failed = 0
        for b in make_batches(hotels, cfg["batch_size"]):
            n = run_batch(client, conn, b, cfg, "eligibility_test", None)
            if n is None:
                failed += len(b)
            else:
                done += n
        return f"Eligibility test: {done} hotels scraped, {failed} failed."
    finally:
        if own:
            conn.close()
