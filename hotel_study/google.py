from collections import defaultdict
import time
from datetime import datetime, timezone

from apify_client import ApifyClient

from hotel_study.config import get_secret, load_config
from hotel_study.db import connect, upsert
from hotel_study.matching import build_query, pick_best

ACTOR = "s-r/free-google-hotels-scraper"


def group_results(items: list[dict]) -> dict[str, list[dict]]:
    """Actor echoes the query on every item as `search_query`."""
    out = defaultdict(list)
    for it in items:
        out[it.get("search_query")].append(it)
    return out


def _search(client, queries: list[str], cfg: dict, conn) -> tuple[list[dict], float]:
    g = cfg["google"]
    run_input = {"queries": queries, "max_results_per_query": g["top_results"], "hl": g["hl"], "gl": g["gl"],
                 "fetch_details": g["fetch_details"]}
    started = datetime.now(timezone.utc)
    run = client.actor(ACTOR).start(run_input=run_input)
    # Provenance first, so an interrupted command can be traced.
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "pass": "google", "input": run_input,
                                  "dataset_id": run.default_dataset_id, "started_at": started}], "run_id")
    conn.commit()
    run = client.run(run.id).wait_for_finish()
    time.sleep(10)  # usage_total_usd is only filled in a few seconds after the run ends
    run = client.run(run.id).get()
    items = list(client.dataset(run.default_dataset_id).iterate_items())
    upsert(conn, "scrape_runs", [{"run_id": run.id, "actor": ACTOR, "finished_at": datetime.now(timezone.utc),
                                  "item_count": len(items), "usage_usd": run.usage_total_usd or 0}], "run_id")
    conn.commit()
    if cfg["delete_remote_datasets"]:
        client.dataset(run.default_dataset_id).delete()
    return items, run.usage_total_usd or 0


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
    done = spent_runs = 0
    with connect() as conn:
        hotels = conn.execute(sql, args).fetchall()
        for i in range(0, len(hotels), cfg["google"]["queries_per_run"]):
            spent = conn.execute("SELECT coalesce(sum(usage_usd), 0) FROM scrape_runs").fetchone()[0]
            if spent >= cfg["max_spend_usd"]:
                return {"stopped": f"max_spend_usd {cfg['max_spend_usd']} reached (spent {spent})", "hotels": done}
            chunk = [(h, "hotels in " + build_query(n, d))  # bare names often return nothing
                     for h, n, d in hotels[i:i + cfg["google"]["queries_per_run"]]]
            items, _ = _search(client, [q for _, q in chunk], cfg, conn)
            by_query = group_results(items)
            now = datetime.now(timezone.utc)
            rows = [{"hotel_id": h, "google_query": q, "google_scraped_at": now,
                     **pick_best(n, by_query.get(q, []), cfg["name_match"])}
                    for (h, q), (_, n, _) in zip(chunk, hotels[i:i + len(chunk)])]
            upsert_rows(conn, rows)
            conn.commit()
            done += len(rows)
            spent_runs += 1
    return {"hotels": done, "runs": spent_runs}


def upsert_rows(conn, rows: list[dict]) -> None:
    # hotels has NOT NULL columns, so UPDATE rather than INSERT-upsert.
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE hotels SET google_query=%(google_query)s, google_name=%(google_name)s, "
            "google_detail_url=%(google_detail_url)s, google_star_rating=%(google_star_rating)s, "
            "google_star_class_label=%(google_star_class_label)s, google_match_score=%(google_match_score)s, "
            "google_scraped_at=%(google_scraped_at)s WHERE hotel_id=%(hotel_id)s", rows)
