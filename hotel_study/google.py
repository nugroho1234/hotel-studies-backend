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


def check_spend(conn, cfg: dict) -> None:
    """Raise if a finished run has no usage recorded, or the spend cap is reached."""
    missing = conn.execute("SELECT count(*) FROM scrape_runs WHERE finished_at IS NOT NULL AND usage_usd IS NULL").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} finished scrape_runs row(s) have NULL usage_usd; fix them before spending more")
    spent = conn.execute("SELECT coalesce(sum(usage_usd), 0) FROM scrape_runs").fetchone()[0]
    if spent >= cfg["max_spend_usd"]:
        raise RuntimeError(f"max_spend_usd {cfg['max_spend_usd']} reached (spent {spent})")


def _wait_for_usage(client, run, n_items: int, g: dict):
    """Usage is filled in seconds after a run ends (0.0 at first), so with items we wait for > 0."""
    waited = 0
    while True:
        run = client.run(run.id).get()
        u = run.usage_total_usd
        if u is not None and (u > 0 or n_items == 0):
            return run, u
        if waited >= g["usage_max_wait_seconds"]:
            return run, None
        time.sleep(g["usage_poll_seconds"])
        waited += g["usage_poll_seconds"]


def _search(client, queries: list[str], cfg: dict, conn):
    """Start the run, record provenance, return (items, dataset_id). Raises on failure; never writes usage 0 for a missing value."""
    g = cfg["google"]
    run_input = {"queries": queries, "max_results_per_query": g["top_results"], "hl": g["hl"], "gl": g["gl"],
                 "fetch_details": g["fetch_details"]}
    run = client.actor(ACTOR).start(run_input=run_input)
    # Provenance first, so an interrupted command can be traced.
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "pass": "google",
                                  "input": run_input | {"query_prefix": g["query_prefix"]},
                                  "dataset_id": run.default_dataset_id, "started_at": datetime.now(timezone.utc)}], "run_id")
    conn.commit()
    run = client.run(run.id).wait_for_finish()
    ok = str(getattr(run.status, "value", run.status)) == "SUCCEEDED"
    items = list(client.dataset(run.default_dataset_id).iterate_items()) if ok else []
    run, usage = _wait_for_usage(client, run, len(items), g)
    row = {"run_id": run.id, "actor": ACTOR, "finished_at": datetime.now(timezone.utc), "item_count": len(items)}
    if usage is not None:
        row["usage_usd"] = usage
    upsert(conn, "scrape_runs", [row], "run_id")
    conn.commit()
    if not ok:
        raise RuntimeError(f"Apify run {run.id} ended with status {run.status}")
    if usage is None:
        raise RuntimeError(f"Apify run {run.id}: usage not reported within {g['usage_max_wait_seconds']}s; usage_usd left NULL")
    return items, run.default_dataset_id


def process_chunk(client, conn, chunk: list[tuple[int, str, str]], cfg: dict) -> int:
    """chunk = [(hotel_id, hotel_name, query)]. Stamps hotels only after a successful, mappable run."""
    items, dataset_id = _search(client, [q for _, _, q in chunk], cfg, conn)
    by_query = group_results(items)
    wanted = {q for _, _, q in chunk}
    unmapped = [it for it in items if it.get("search_query") not in wanted]
    if items and len(unmapped) == len(items):
        raise RuntimeError("No Apify item maps to any query of this chunk; hotels left unstamped")
    if unmapped:
        log.warning("%d of %d Apify items did not map to a query", len(unmapped), len(items))
    now = datetime.now(timezone.utc)
    rows = [{"hotel_id": h, "google_query": q, "google_scraped_at": now,
             **pick_best(n, by_query.get(q, []), cfg["name_match"])} for h, n, q in chunk]
    upsert_rows(conn, rows)
    conn.commit()
    if cfg["delete_remote_datasets"]:  # only after the results are safely stored
        client.dataset(dataset_id).delete()
    return len(rows)


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
            check_spend(conn, cfg)
            chunk = [(h, name, cfg["google"]["query_prefix"] + build_query(name, d)) for h, name, d in hotels[i:i + n]]
            done += process_chunk(client, conn, chunk, cfg)
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
