import logging

import pandas as pd

from hotel_study.cli import same_destination
from hotel_study.config import load_config
from hotel_study.db import connect, upsert
from hotel_study.parsing import parse_hotel_id

log = logging.getLogger(__name__)


def build_rows(df: pd.DataFrame, threshold: int, limit: int | None = None,
               destination: str | None = None) -> tuple[list[dict], dict]:
    """Pure: CSV frame -> hotels rows (CSV-derived columns only) + counts."""
    counts = {"loaded": 0, "skipped_no_id": 0, "skipped_bad_value": 0, "duplicates_merged": 0}
    rows: dict[int, dict] = {}
    for i, r in df.iterrows():
        if limit is not None and len(rows) >= limit:
            break
        line = i + 2  # CSV line number (header is line 1)
        if not same_destination(r["destination"] or "", destination):
            continue
        hid = parse_hotel_id(r["review_url"])
        if hid is None:
            log.warning("CSV row %d skipped: no hotel id in review_url", line)
            counts["skipped_no_id"] += 1
            continue
        if str(r.get("hotel_id") or "").strip() not in ("", str(hid)):
            log.warning("CSV row %d: hotel_id column %s != id in url %d", line, r["hotel_id"], hid)
        try:
            star = int(r["star_class"])
            n = int(r["num_reviews"]) if (r["num_reviews"] or "").strip() else None
        except ValueError:
            log.warning("CSV row %d skipped: bad star_class/num_reviews", line)
            counts["skipped_bad_value"] += 1
            continue
        if hid in rows:
            counts["duplicates_merged"] += 1
            if rows[hid]["star_class"] != star:
                rows[hid]["star_check_status"] = "needs_manual_check"
            continue
        rows[hid] = {
            "hotel_id": hid, "date_scraped": r["date_scraped"] or None, "hotel_name": r["hotel_name"],
            "destination": r["destination"], "star_class": star, "num_reviews": n,
            "review_url": r["review_url"], "address": r["address"] or None,
            "num_reviews_gt_100": n is not None and n > threshold,
        }
    counts["loaded"] = len(rows)
    return list(rows.values()), counts


def load_hotels(csv_path, limit: int | None = None, destination: str | None = None) -> dict:
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    rows, counts = build_rows(df, load_config()["prescreen"]["num_reviews_gt"], limit, destination)
    # Separate calls so rows without a flag never overwrite star_check_status (upsert uses rows[0] keys).
    plain = [r for r in rows if "star_check_status" not in r]
    flagged = [r for r in rows if "star_check_status" in r]
    with connect() as conn:
        upsert(conn, "hotels", plain, "hotel_id")
        upsert(conn, "hotels", flagged, "hotel_id")
        conn.commit()
    return counts
