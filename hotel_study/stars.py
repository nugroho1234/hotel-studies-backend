import csv
from pathlib import Path

from hotel_study.config import ROOT, load_config
from hotel_study.db import connect

OUT = ROOT / "output"
NEEDS_CHECK = ("match_uncertain", "needs_manual_check")
MANUAL_COLS = ("match_confirmed", "manual_google_star", "ta_displayed_class")


class OpenManualChecks(Exception):
    """items = [(hotel_id, reason)]; refuses finalise before anything is written."""

    def __init__(self, items: list[tuple], summary: str = ""):
        self.items = items
        super().__init__("finalise-stars refused. " + summary + "\n" + "\n".join(f"  {i}: {r}" for i, r in items))


def star_status(google: int | None, ta: int, match_score: float | None, accept_threshold: float) -> tuple[str, int | None]:
    """PRD §6 table, first match wins. match_score None = no match to doubt (no Google hit, or already resolved)."""
    if match_score is not None and match_score < accept_threshold:
        return "match_uncertain", None
    if google is None:
        return "google_missing", ta
    diff = google - ta
    if diff == 0:
        return "agree", ta
    return ("needs_manual_check", None) if diff == 1 else ("disagree", None)


def _int_cell(s: str) -> int:
    f = float(s)
    if not f.is_integer() or not 1 <= f <= 5:
        raise ValueError(f"{s!r} is not a star class 1-5")
    return int(f)


def _resolve(row: dict, uncertain: bool) -> tuple[str, int | None, int | None, float | None]:
    """-> (status, final, star_diff, ta_displayed_class used). Raises ValueError for an open or invalid cell."""
    cell = lambda k: (row.get(k) or "").strip().lower()
    star, google, mc = row["star_class"], row["google_star_rating"], cell("match_confirmed")
    if mc not in ("", "y", "n") or (uncertain and not mc):
        raise ValueError(f"match_confirmed must be y or n, got {mc!r}")
    if mc == "n":
        google = _int_cell(cell("manual_google_star")) if cell("manual_google_star") else None
    status, final = star_status(google, star, None, accept_threshold=0)  # score None: match already resolved, threshold unused
    diff = None if google is None else google - star
    ta = None
    if status == "needs_manual_check":
        try:
            ta = float(cell("ta_displayed_class"))
        except ValueError:
            raise ValueError(f"ta_displayed_class {cell('ta_displayed_class')!r} is blank or not a number") from None
        if not 1 <= ta <= 5 or ta * 2 != int(ta * 2):
            raise ValueError(f"ta_displayed_class {ta} must be x.0 or x.5")
        status, final = ("agree_half_star", star) if ta == star + 0.5 else ("disagree", None)
    return status, final, diff, ta


def resolve_manual(row: dict, uncertain: bool = False) -> tuple[str, int | None]:
    return _resolve(row, uncertain)[:2]


def plan_finalise(csv_rows: list[dict], hotels: dict[int, dict], all_ids: set[int], accept: float, check_unchecked: bool = True) -> list[dict]:
    """hotels = the hotels in scope, keyed by id. Returns UPDATE dicts, or raises OpenManualChecks."""
    unchecked = [h for h, v in hotels.items() if v["num_reviews_ge_100"] and v["star_check_status"] is None]
    if check_unchecked and unchecked:  # skipped for --limit/--destination test runs
        raise OpenManualChecks([(h, "pre-screened but not yet checked") for h in unchecked],
                               f"{len(unchecked)} pre-screened hotel(s) not yet checked; run google-stars and compare-stars first.")
    errors, by_id = [], {}
    for r in csv_rows:
        try:
            i = int(r["hotel_id"])
        except (ValueError, TypeError, KeyError):
            errors.append((r.get("hotel_id"), "invalid hotel_id"))
            continue
        if i not in all_ids:
            errors.append((i, "hotel_id not in the database"))
        elif i in hotels:
            by_id[i] = r
    updates = []
    for i, h in hotels.items():
        if not h["google_scraped"]:
            continue
        status, _ = star_status(h["google_star_rating"], h["star_class"], h["google_match_score"], accept)
        if status not in NEEDS_CHECK:
            continue
        if i not in by_id:
            errors.append((i, "needs a manual check but is missing from the CSV"))
            continue
        try:
            st, final, diff, ta = _resolve(h | {k: by_id[i].get(k) for k in MANUAL_COLS}, status == "match_uncertain")
        except ValueError as e:
            errors.append((i, str(e)))
            continue
        updates.append({"hotel_id": i, "star_check_status": st, "final_star_rating": final, "star_diff": diff, "ta_displayed_class": ta})
    if errors:
        raise OpenManualChecks(errors)
    return updates


def _scope(destination, limit) -> tuple[str, list, str]:
    where, args = "TRUE", []
    if destination:
        where, args = "lower(destination) = lower(%s)", [destination.strip()]
    return where, args, " LIMIT %s" if limit else ""


COLS = "hotel_id, hotel_name, destination, star_class, google_name, google_star_rating, google_match_score, google_scraped_at, num_reviews_ge_100, star_check_status"


def _has_manual_work(path: Path) -> bool:
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8-sig") as f:
        return any((r.get(c) or "").strip() for r in csv.DictReader(f) for c in MANUAL_COLS)


def compare_stars(limit: int | None = None, destination: str | None = None, overwrite: bool = False) -> dict:
    if not overwrite and _has_manual_work(OUT / "manual_checks.csv"):
        raise RuntimeError("output/manual_checks.csv already contains manual entries; rerunning would destroy them. Pass --overwrite to replace it.")
    accept = load_config()["name_match"]["accept"]
    where, args, lim = _scope(destination, limit)
    sql = f"SELECT {COLS} FROM hotels WHERE google_scraped_at IS NOT NULL AND {where} ORDER BY hotel_id{lim}"
    rows, upd = [], []
    with connect() as conn:
        cur = conn.execute(sql, args + ([limit] if limit else []))
        names = [d.name for d in cur.description]
        for rec in cur.fetchall():
            h = dict(zip(names, rec))
            status, final = star_status(h["google_star_rating"], h["star_class"], h["google_match_score"], accept)
            g = h["google_star_rating"]
            diff = None if g is None else g - h["star_class"]
            upd.append({"hotel_id": h["hotel_id"], "star_diff": diff, "star_check_status": status, "final_star_rating": final, "in_sample_frame": final is not None})
            rows.append(h | {"star_diff": diff, "star_check_status": status})
        with conn.cursor() as c:
            c.executemany("UPDATE hotels SET star_diff=%(star_diff)s, star_check_status=%(star_check_status)s, "
                          "final_star_rating=%(final_star_rating)s, in_sample_frame=%(in_sample_frame)s WHERE hotel_id=%(hotel_id)s", upd)
        conn.commit()
    OUT.mkdir(exist_ok=True)
    rows.sort(key=lambda r: (r["star_check_status"] not in NEEDS_CHECK, r["destination"], r["hotel_id"]))
    _write(OUT / "star_comparison.csv", rows, ["hotel_id", "hotel_name", "destination", "star_class", "google_name",
           "google_star_rating", "google_match_score", "star_diff", "star_check_status"])
    checks = [r | {"match_confirmed": "", "manual_google_star": "", "ta_displayed_class": ""} for r in rows if r["star_check_status"] in NEEDS_CHECK]
    _write(OUT / "manual_checks.csv", checks, ["hotel_id", "hotel_name", "destination", "star_class", "google_name", "google_star_rating",
           "star_diff", "star_check_status", "match_confirmed", "manual_google_star", "ta_displayed_class"])
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["star_check_status"]] = counts.get(r["star_check_status"], 0) + 1
    return {"checked": len(rows), "manual_checks": len(checks), **counts}


def _write(path: Path, rows: list[dict], cols: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def finalise_stars(csv_path: Path, limit: int | None = None, destination: str | None = None) -> dict:
    accept = load_config()["name_match"]["accept"]
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        csv_rows = list(csv.DictReader(f))
    where, args, lim = _scope(destination, limit)
    with connect() as conn:
        cur = conn.execute(f"SELECT {COLS} FROM hotels WHERE {where} ORDER BY hotel_id{lim}", args + ([limit] if limit else []))
        names = [d.name for d in cur.description]
        hotels = {}
        for rec in cur.fetchall():
            h = dict(zip(names, rec))
            h["google_scraped"] = h["google_scraped_at"] is not None
            hotels[h["hotel_id"]] = h
        all_ids = {r[0] for r in conn.execute("SELECT hotel_id FROM hotels").fetchall()}
        updates = plan_finalise(csv_rows, hotels, all_ids, accept, check_unchecked=not (limit or destination))  # raises before any write
        with conn.cursor() as c:
            c.executemany("UPDATE hotels SET star_check_status=%(star_check_status)s, final_star_rating=%(final_star_rating)s, "
                          "star_diff=%(star_diff)s, ta_displayed_class=%(ta_displayed_class)s WHERE hotel_id=%(hotel_id)s", updates)
            c.execute("UPDATE hotels SET in_sample_frame = (final_star_rating IS NOT NULL) WHERE hotel_id = ANY(%s)", [list(hotels)])
        conn.commit()
    return {"hotels": len(hotels), "resolved": len(updates)}
