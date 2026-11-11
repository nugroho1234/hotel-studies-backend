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
    row = conn.execute("SELECT run_id FROM scrape_runs WHERE finished_at IS NOT NULL AND usage_usd IS NULL LIMIT 1").fetchone()
    if row:
        raise RuntimeError(f"scrape_runs row {row[0]} is finished but has NULL usage_usd. Read its cost in the Apify console, then run: "
                           f"update scrape_runs set usage_usd = <value> where run_id = '{row[0]}'")
    spent = conn.execute("SELECT coalesce(sum(usage_usd), 0) FROM scrape_runs").fetchone()[0]
    if spent >= cfg["max_spend_usd"]:
        raise RuntimeError(f"max_spend_usd {cfg['max_spend_usd']} reached (spent {spent})")


def _wait_for_usage(client, run, g: dict):
    """Apify reports usage 0.0 right after a run ends, so poll until > 0 or the max wait; returns the last observed value."""
    waited = 0
    while True:
        run = client.run(run.id).get()
        u = run.usage_total_usd
        if (u is not None and u > 0) or waited >= g["usage_max_wait_seconds"]:
            return run, u
        time.sleep(g["usage_poll_seconds"])
        waited += g["usage_poll_seconds"]


def form_query(form: str, name: str, destination: str, g: dict) -> str:
    """Names of the ladder stages (google.query_forms) -> query text."""
    if form == "name_only":
        return name
    return (g["query_prefix"] if form == "prefixed" else "") + build_query(name, destination)


def is_miss(pick: dict | None, cfg: dict) -> bool:
    """No result, or best score below name_match.uncertain. 75-89 is not a miss."""
    return pick is None or pick["google_match_score"] < cfg["name_match"]["uncertain"]


def _search(client, queries: list[str], cfg: dict, conn, form: str = "plain", allow_empty: bool = False):
    """Start the run, record provenance, return (items, dataset_id). Raises on failure; never writes usage 0 for an unconfirmed run that returned items."""
    g = cfg["google"]
    run_input = {"queries": queries, "max_results_per_query": g["top_results"], "hl": g["hl"], "gl": g["gl"],
                 "fetch_details": g["fetch_details"]}
    run = client.actor(ACTOR).start(run_input=run_input)
    # Provenance first, so an interrupted command can be traced.
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "pass": "google",
                                  "input": run_input | {"query_form": form, "query_prefix": g["query_prefix"]},
                                  "dataset_id": run.default_dataset_id, "started_at": datetime.now(timezone.utc)}], "run_id")
    conn.commit()
    run = client.run(run.id).wait_for_finish()
    ok = str(getattr(run.status, "value", run.status)) == "SUCCEEDED"
    items = list(client.dataset(run.default_dataset_id).iterate_items()) if ok else []
    run, usage = _wait_for_usage(client, run, g)
    confirmed = usage is not None and usage > 0
    row = {"run_id": run.id, "actor": ACTOR, "finished_at": datetime.now(timezone.utc), "item_count": len(items)}
    if confirmed or (not items and usage is not None):  # zero-item/failed runs may genuinely cost 0
        row["usage_usd"] = usage
    if not confirmed:
        log.warning("Apify run %s: usage unconfirmed after %ss (observed %s)", run.id, g["usage_max_wait_seconds"], usage)
    upsert(conn, "scrape_runs", [row], "run_id")
    conn.commit()
    if not ok:
        raise RuntimeError(f"Apify run {run.id} ended with status {run.status}")
    if not items:
        if not allow_empty:
            raise RuntimeError(f"Apify run {run.id} returned no items; hotels left unstamped (check the actor and queries)")
        return [], run.default_dataset_id
    if not confirmed:
        raise RuntimeError(f"Apify run {run.id}: usage not confirmed within {g['usage_max_wait_seconds']}s; usage_usd left NULL")
    return items, run.default_dataset_id


def process_chunk(client, conn, hotels: list[tuple[int, str, str]], cfg: dict) -> int:
    """hotels = [(hotel_id, name, destination)]. Runs the query ladder (only misses go on), stamps hotels once at the end."""
    g = cfg["google"]
    best = {h: None for h, _, _ in hotels}  # hotel_id -> (pick, query)
    last_q, datasets = {}, []
    for stage, form in enumerate(g["query_forms"]):
        pending = [(h, n, form_query(form, n, d, g)) for h, n, d in hotels
                   if (best[h] is None or is_miss(best[h][0], cfg)) and form_query(form, n, d, g) != last_q.get(h)]
        if not pending:
            continue
        items, dataset_id = _search(client, [q for _, _, q in pending], cfg, conn, form, allow_empty=stage > 0)
        datasets.append(dataset_id)
        by_query = group_results(items)
        unmapped = [it for it in items if it.get("search_query") not in {q for _, _, q in pending}]
        if items and len(unmapped) == len(items) and stage == 0:
            raise RuntimeError("No Apify item maps to any query of this chunk; hotels left unstamped")
        if unmapped:
            log.warning("%d of %d Apify items did not map to a query", len(unmapped), len(items))
        for h, n, q in pending:
            last_q[h] = q
            pick = pick_best(n, by_query.get(q, []), cfg["name_match"])
            if best[h] is None or pick["google_match_score"] > best[h][0]["google_match_score"]:
                best[h] = (pick, q)
    now = datetime.now(timezone.utc)
    rows = [{"hotel_id": h, "google_query": best[h][1] if best[h][0]["google_match_score"] > 0 else last_q[h],
             "google_scraped_at": now, **best[h][0]} for h, _, _ in hotels]
    upsert_rows(conn, rows)
    conn.commit()
    if cfg["delete_remote_datasets"]:  # only after the results are safely stored
        for d in datasets:
            client.dataset(d).delete()
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
