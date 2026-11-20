import random
from pathlib import Path

import pandas as pd

from hotel_study.cli import same_destination
from hotel_study.config import ROOT, load_config
from hotel_study.db import connect

SUBRATINGS = ["subrating_value", "subrating_rooms", "subrating_location", "subrating_cleanliness",
              "subrating_service", "subrating_sleep_quality"]
EXPORT_COLS = ["review_id", "hotel_id", "hotel_name", "destination", "final_star_rating", "published_date",
               "language", "rating", "trip_info_trip_type", *SUBRATINGS, "title", "text"]
SELECT_COLS = EXPORT_COLS  # nothing else is read: no user hash, hometown or owner response


def allocate(strata_sizes: dict, total: int = 100) -> dict:
    """Equal split; remainder to the strata with most capacity; a stratum below its share gets all it has
    and the shortfall is re-split among the rest. Sum is `total` unless pooled capacity is smaller."""
    out, active, left = {}, dict(strata_sizes), total
    while active and left > 0:
        base, rem = divmod(left, len(active))
        order = sorted(active, key=lambda k: (-active[k], str(k)))
        share = {k: base + (i < rem) for i, k in enumerate(order)}
        short = [k for k in active if active[k] < share[k]]
        if not short:
            out.update(share)
            break
        for k in short:
            out[k] = active.pop(k)
            left -= out[k]
    return {k: out.get(k, 0) for k in strata_sizes}


def _greedy(rows: list, n: int, per_hotel: dict, cap: int) -> list:
    """Take rows in order until n, skipping hotels already at the cap."""
    got = []
    for r in rows:
        if len(got) == n:
            break
        if per_hotel.get(r["hotel_id"], 0) < cap:
            per_hotel[r["hotel_id"]] = per_hotel.get(r["hotel_id"], 0) + 1
            got.append(r)
    return got


def capacity(rows: list, max_per_hotel: int) -> int:
    """Most reviews drawable from rows: sum over hotels of min(reviews, max_per_hotel)."""
    n: dict = {}
    for r in rows:
        n[r["hotel_id"]] = n.get(r["hotel_id"], 0) + 1
    return sum(min(c, max_per_hotel) for c in n.values())


def draw_pilot(df: pd.DataFrame, seed: int, max_per_hotel: int, min_id: int, total: int = 100) -> pd.DataFrame:
    """Stratified draw (destination|star), equal allocation, at most max_per_hotel per hotel.
    1) capacity of a stratum = sum over hotels of min(reviews, max_per_hotel); allocate() sets its quota.
    2) reserve min_id Indonesian picks: spread over strata with allocate(), limited by each stratum's
       Indonesian capacity and quota; 3) fill each stratum to quota from its remaining reviews.
    Reviews are sorted by review_id and shuffled with a local Random(seed), so row order never matters."""
    rows = df.sort_values("review_id").to_dict("records")
    strata: dict[str, list] = {}
    for r in rows:
        strata.setdefault(f"{r['destination']}|{r['final_star_rating']}", []).append(r)

    cap_of = lambda rs: capacity(rs, max_per_hotel)  # noqa: E731

    quota = allocate({k: cap_of(v) for k, v in strata.items()}, total)
    id_cap = {k: min(cap_of([r for r in v if r["language"] == "id"]), quota[k]) for k, v in strata.items()}
    id_quota = allocate(id_cap, min_id)
    rng, picked = random.Random(seed), []
    for k in sorted(strata):
        rs = strata[k][:]
        rng.shuffle(rs)
        per_hotel: dict = {}
        ids = _greedy([r for r in rs if r["language"] == "id"], id_quota[k], per_hotel, max_per_hotel)
        taken = {r["review_id"] for r in ids}
        rest = _greedy([r for r in rs if r["review_id"] not in taken], quota[k] - len(ids), per_hotel, max_per_hotel)
        picked += [{**r, "pilot_stratum": k} for r in ids + rest]
    return pd.DataFrame(picked, columns=[*df.columns, "pilot_stratum"]).sort_values("review_id").reset_index(drop=True)



def summarise(df: pd.DataFrame, src: pd.DataFrame, cfg: dict) -> str:
    cap_n, n_id = cfg["pilot_max_per_hotel"], int((df.language == "id").sum())
    cap = (f"Per-hotel cap: {cap_n} (pilot_max_per_hotel; deviation from the registered 2)" if cap_n != 2
           else "Per-hotel cap: 2 (pilot_max_per_hotel, as registered)")
    strata = ", ".join(f"{k}: {n}" for k, n in df.groupby("pilot_stratum").size().items())
    lines = [f"Pilot: {len(df)} reviews in {df.pilot_stratum.nunique()} strata ({strata}).",
             f"Indonesian: {n_id}. Max reviews per hotel: {int(df.groupby('hotel_id').size().max()) if len(df) else 0}. {cap}"]
    rows = src.to_dict("records")
    if len(df) < cfg["pilot_size"]:
        lines.append(f"SHORT of target (capacity): got {len(df)} of {cfg['pilot_size']}; pooled capacity under the per-hotel cap is {capacity(rows, cap_n)}.")
    if n_id < cfg["pilot_min_id"]:
        id_cap = capacity([r for r in rows if r["language"] == "id"], cap_n)
        lines.append(f"SHORT of Indonesian minimum: got {n_id} of {cfg['pilot_min_id']}; "
                     + (f"capacity is the reason (only {id_cap} drawable)." if id_cap < cfg["pilot_min_id"]
                        else "capacity is not the reason (stratum quotas limited it)."))
    return "\n".join(lines)


def pilot_sample(force: bool = False, limit: int | None = None, destination: str | None = None,
                 conn=None, out_dir: Path | None = None) -> str:
    """Flag and export the pilot reviews. Refuses if a pilot already exists; --force clears it and redraws."""
    cfg = load_config()
    if force and (limit or destination):
        raise RuntimeError("pilot-sample refused: --force clears every pilot flag, so it cannot be combined with --limit or --destination")
    own = conn is None
    conn = conn or connect()
    try:
        if conn.execute("SELECT 1 FROM reviews WHERE is_pilot LIMIT 1").fetchone() and not force:
            raise RuntimeError("pilot-sample refused: a pilot sample already exists; --force redraws it")
        rows = conn.execute(
            "SELECT r.review_id, r.hotel_id, h.hotel_name, h.destination, h.final_star_rating, r.published_date, "
            "r.language, r.rating, r.trip_info_trip_type, r.subrating_value, r.subrating_rooms, r.subrating_location, "
            "r.subrating_cleanliness, r.subrating_service, r.subrating_sleep_quality, r.title, r.text "
            "FROM reviews r JOIN hotels h USING (hotel_id) "
            "WHERE r.is_selected AND h.in_final_sample AND h.scrape_status = 'complete' ORDER BY r.review_id").fetchall()
        df = pd.DataFrame(rows, columns=SELECT_COLS)
        df = df[df.destination.map(lambda d: same_destination(d or "", destination))]
        if limit:
            df = df[df.hotel_id.isin(sorted(df.hotel_id.unique())[:limit])]
        out = draw_pilot(df, cfg["seed"], cfg["pilot_max_per_hotel"], cfg["pilot_min_id"], cfg["pilot_size"])
        conn.execute("UPDATE reviews SET is_pilot = false, pilot_stratum = NULL WHERE is_pilot OR pilot_stratum IS NOT NULL")
        for k, g in out.groupby("pilot_stratum"):
            conn.execute("UPDATE reviews SET is_pilot = true, pilot_stratum = %s WHERE review_id = ANY(%s)",
                         (k, [int(i) for i in g.review_id]))
        conn.commit()
        path = (out_dir or ROOT / "output") / "pilot_100.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        out[EXPORT_COLS].to_csv(path, index=False)
        return f"{summarise(out, df, cfg)} Wrote {path}."
    finally:
        if own:
            conn.close()


def pilot_export(out_dir: Path | None = None) -> str:
    """Rebuild table pilot_sample (pilot_id randomised with the study seed; review_id references reviews) and write the coder CSVs."""
    cfg = load_config()
    out = out_dir or ROOT / "output"
    with connect() as conn:
        rows = conn.execute("SELECT r.review_id, r.pilot_stratum, h.hotel_id, h.hotel_name, h.destination, h.final_star_rating, r.published_date, "
                            "r.language, r.rating, r.trip_info_trip_type, r.title, r.text FROM reviews r JOIN hotels h USING (hotel_id) "
                            "WHERE r.is_pilot ORDER BY r.review_id").fetchall()
        if not rows:
            raise RuntimeError("no pilot reviews; run pilot-sample first")
        order = list(range(len(rows)))
        random.Random(cfg["seed"]).shuffle(order)  # reviews are sorted by id first, so the labels are reproducible
        labelled = [(pid, rows[i]) for pid, i in enumerate(order, 1)]
        conn.execute("DELETE FROM pilot_sample")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO pilot_sample (pilot_id, review_id, stratum) VALUES (%s, %s, %s)", [(p, r[0], r[1]) for p, r in labelled])
        conn.commit()
    labelled.sort()
    cols = ["review_id", "pilot_stratum", "hotel_id", "hotel_name", "destination", "final_star_rating", "published_date", "language", "rating", "trip_type", "title", "text"]
    pd.DataFrame([(p, *r) for p, r in labelled], columns=["pilot_id", *cols]).to_csv(out / "pilot_sample.csv", index=False)
    pd.DataFrame([(p, r[11], r[7]) for p, r in labelled], columns=["pilot_id", "review_text", "language"]).to_csv(out / "pilot_coding.csv", index=False)
    return f"{len(labelled)} pilot reviews: table pilot_sample, {out / 'pilot_sample.csv'}, {out / 'pilot_coding.csv'}"
