import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from apify_client import ApifyClient

from hotel_study.config import get_secret, load_config
from hotel_study.db import connect, upsert
from hotel_study.matching import build_query, pick_best

ACTOR = "s-r/free-google-hotels-scraper"
log = logging.getLogger(__name__)


def group_results(items: list[dict]) -> dict[str, list[dict]]:
    """Actor echoes the query on every item as `search_query`."""
    out = defaultdict(list)
    for it in items:
        out[it.get("search_query")].append(it)
    return out


class SpendLimitReached(RuntimeError):
    """No further paid run may start (cap reached, or a run's cost is unknown)."""


def check_spend(conn, cfg: dict) -> None:
    """Raise if any run is unfinished or has no usage recorded (cost unknown), or the spend cap is reached."""
    row = conn.execute("SELECT run_id FROM scrape_runs WHERE finished_at IS NULL OR usage_usd IS NULL LIMIT 1").fetchone()
    if row:
        raise SpendLimitReached(f"scrape_runs row {row[0]} is unfinished or has NULL usage_usd. Rerun scrape-reviews to reconcile it, or read its cost in "
                                f"the Apify console and run: update scrape_runs set finished_at = now(), usage_usd = <value> where run_id = '{row[0]}'")
    spent = conn.execute("SELECT coalesce(sum(usage_usd), 0) FROM scrape_runs").fetchone()[0]
    if spent >= cfg["max_spend_usd"]:
        raise SpendLimitReached(f"max_spend_usd {cfg['max_spend_usd']} reached (spent {spent})")


def _wait_for_usage(client, run, max_wait: int, poll: int):
    """Apify reports usage 0.0 right after a run ends, so poll until > 0 or the max wait; returns the last observed value."""
    waited = 0
    while True:
        run = client.run(run.id).get()
        u = run.usage_total_usd
        if (u is not None and u > 0) or waited >= max_wait:
            return run, u
        time.sleep(poll)
        waited += poll


FORMS = {"plain", "name_only", "prefixed"}


def form_query(form: str, name: str, destination: str, g: dict) -> str:
    """Names of the ladder stages (google.query_forms) -> query text."""
    if form not in FORMS:
        raise ValueError(f"unknown query form {form!r}")
    if form == "name_only":
        return name
    return (g["query_prefix"] if form == "prefixed" else "") + build_query(name, destination)


def is_miss(pick: dict | None, cfg: dict) -> bool:
    """No result, or best score below name_match.uncertain. 75-89 is not a miss."""
    return pick is None or pick["google_match_score"] < cfg["name_match"]["uncertain"]


def _search(client, queries: list[str], cfg: dict, conn, form: str = "plain"):
    """Start the run, record provenance, return (items, dataset_id). Raises on failure; never writes usage 0 for an unconfirmed run that returned items."""
    g = cfg["google"]
    run_input = {"queries": queries, "max_results_per_query": g["top_results"], "hl": g["hl"], "gl": g["gl"],
                 "fetch_details": g["fetch_details"]}
    run = client.actor(ACTOR).start(run_input=run_input)
    # Provenance first, so an interrupted command can be traced.
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "pass": "google",
                                  "input": run_input | {"query_form": form, "query_forms": g["query_forms"], "query_prefix": g["query_prefix"]},
                                  "dataset_id": run.default_dataset_id, "started_at": datetime.now(timezone.utc)}], "run_id")
    conn.commit()
    run = client.run(run.id).wait_for_finish()
    ok = str(getattr(run.status, "value", run.status)) == "SUCCEEDED"
    items = list(client.dataset(run.default_dataset_id).iterate_items()) if ok else []
    # zero-item runs only get the short wait: their usage is genuinely ~0 and ~1800 runs must stay fast
    wait = g["usage_max_wait_seconds"] if items else g["usage_zero_item_wait_seconds"]
    run, usage = _wait_for_usage(client, run, wait, g["usage_poll_seconds"])
    confirmed = usage is not None and usage > 0
    row = {"run_id": run.id, "actor": ACTOR, "finished_at": datetime.now(timezone.utc), "item_count": len(items)}
    if confirmed or (not items and usage is not None):  # zero-item/failed runs may genuinely cost 0
        row["usage_usd"] = usage
    if not confirmed and items:
        log.warning("Apify run %s: usage unconfirmed after %ss (observed %s)", run.id, g["usage_max_wait_seconds"], usage)
    upsert(conn, "scrape_runs", [row], "run_id")
    conn.commit()
    if not ok:
        raise RuntimeError(f"Apify run {run.id} ended with status {run.status}")
    if not items:  # normal outcome: every hotel of the stage is a miss
        log.info("Apify run %s (%s) returned no items for %d queries", run.id, form, len(queries))
        return [], run.default_dataset_id
    if not confirmed:
        raise RuntimeError(f"Apify run {run.id}: usage not confirmed within {g['usage_max_wait_seconds']}s; usage_usd left NULL")
    return items, run.default_dataset_id


def process_chunk(client, conn, hotels: list[tuple[int, str, str]], cfg: dict) -> int:
    """hotels = [(hotel_id, name, destination)]. Runs the query ladder; hotels resolved at a stage are stamped at once,
    only misses go on, and the leftovers are stamped with their best result at the end. A failed stage keeps earlier stamps."""
    g = cfg["google"]
    forms = g["query_forms"]
    if bad := [f for f in forms if f not in FORMS]:
        raise ValueError(f"unknown google.query_forms entries {bad}; allowed {sorted(FORMS)}")
    best = {h: None for h, _, _ in hotels}  # hotel_id -> (pick, query)
    last_q, stamped = {}, set()

    def stamp(ids, dataset_id=None):
        now = datetime.now(timezone.utc)
        rows = [{"hotel_id": h, "google_query": best[h][1] if best[h][0]["google_match_score"] > 0 else last_q[h],
                 "google_scraped_at": now, **best[h][0]} for h in ids]
        if rows:
            upsert_rows(conn, rows)
            conn.commit()
            stamped.update(ids)
        if dataset_id and cfg["delete_remote_datasets"]:  # only after the results are safely stored
            client.dataset(dataset_id).delete()

    try:
        for stage, form in enumerate(forms):
            pending = [(h, n, form_query(form, n, d, g)) for h, n, d in hotels
                       if (best[h] is None or is_miss(best[h][0], cfg)) and form_query(form, n, d, g) != last_q.get(h)]
            if not pending:
                continue
            check_spend(conn, cfg)
            items, dataset_id = _search(client, [q for _, _, q in pending], cfg, conn, form)
            by_query = group_results(items)
            wanted = {q for _, _, q in pending}
            unmapped = [it for it in items if it.get("search_query") not in wanted]
            if items and len(unmapped) == len(items) and stage == 0:
                raise RuntimeError("No Apify item maps to any query of this chunk; hotels left unstamped")
            if unmapped:
                log.warning("%d of %d Apify items did not map to a query", len(unmapped), len(items))
            for h, n, q in pending:
                last_q[h] = q
                pick = pick_best(n, by_query.get(q, []), cfg["name_match"])
                if best[h] is None or pick["google_match_score"] > best[h][0]["google_match_score"]:
                    best[h] = (pick, q)
            stamp([h for h, _, _ in pending if not is_miss(best[h][0], cfg)], dataset_id)
        stamp([h for h, _, _ in hotels if h not in stamped])
    except Exception:
        log.warning("google ladder failed: %d hotels saved, %d left unstamped", len(stamped), len(hotels) - len(stamped))
        raise
    return len(stamped)


def run_google_stars(limit: int | None = None, destination: str | None = None) -> dict:
    cfg = load_config()
    client = ApifyClient(get_secret("APIFY_TOKEN"))
    where = "google_scraped_at IS NULL"  # already-processed hotels are skipped
    if cfg["google_prescreen_only"]:
        where += " AND num_reviews_ge_100"
    args = []
    if destination:
        where += " AND lower(destination) = lower(%s)"
        args.append(destination.strip())
    sql = f"SELECT hotel_id, hotel_name, destination FROM hotels WHERE {where} ORDER BY hotel_id"
    if limit:
        sql += " LIMIT %s"
        args.append(limit)
    done = runs = 0
    n = cfg["google"]["queries_per_run"]
    with connect() as conn:
        hotels = conn.execute(sql, args).fetchall()
        for i in range(0, len(hotels), n):
            done += process_chunk(client, conn, hotels[i:i + n], cfg)
            runs += 1
    return {"hotels": done, "runs": runs}


def upsert_rows(conn, rows: list[dict]) -> None:
    # hotels has NOT NULL columns, so UPDATE rather than INSERT-upsert.
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE hotels SET google_query=%(google_query)s, google_name=%(google_name)s, "
            "google_detail_url=%(google_detail_url)s, google_star_rating=%(google_star_rating)s, "
            "google_star_class_label=%(google_star_class_label)s, google_match_score=%(google_match_score)s, "
            "google_scraped_at=%(google_scraped_at)s WHERE hotel_id=%(hotel_id)s", rows)
