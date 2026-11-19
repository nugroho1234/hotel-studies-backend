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


def draw_pilot(df: pd.DataFrame, seed: int, max_per_hotel: int, min_id: int, total: int | None = None) -> pd.DataFrame:
    """Stratified draw (destination|star), equal allocation, at most max_per_hotel per hotel.
    1) capacity of a stratum = sum over hotels of min(reviews, max_per_hotel); allocate() sets its quota.
    2) reserve min_id Indonesian picks: spread over strata with allocate(), limited by each stratum's
       Indonesian capacity and quota; 3) fill each stratum to quota from its remaining reviews.
    Reviews are sorted by review_id and shuffled with a local Random(seed), so row order never matters."""
    rows = df.sort_values("review_id").to_dict("records")
    strata: dict[str, list] = {}
    for r in rows:
        strata.setdefault(f"{r['destination']}|{r['final_star_rating']}", []).append(r)

    def cap_of(rs):
        n: dict = {}
        for r in rs:
            n[r["hotel_id"]] = n.get(r["hotel_id"], 0) + 1
        return sum(min(c, max_per_hotel) for c in n.values())

    quota = allocate({k: cap_of(v) for k, v in strata.items()}, load_config()["pilot_size"] if total is None else total)
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



def summarise(df: pd.DataFrame, max_per_hotel: int) -> str:
    cap = (f"Per-hotel cap: {max_per_hotel} (pilot_max_per_hotel; deviation from the registered 2)" if max_per_hotel != 2
           else "Per-hotel cap: 2 (pilot_max_per_hotel, as registered)")
    strata = ", ".join(f"{k}: {n}" for k, n in df.groupby("pilot_stratum").size().items())
    return (f"Pilot: {len(df)} reviews in {df.pilot_stratum.nunique()} strata ({strata}). "
            f"Indonesian: {int((df.language == 'id').sum())}. "
            f"Max reviews per hotel: {int(df.groupby('hotel_id').size().max()) if len(df) else 0}. {cap}")


def pilot_sample(force: bool = False, limit: int | None = None, destination: str | None = None,
                 conn=None, out_dir: Path | None = None) -> str:
    """Flag and export the pilot reviews. Refuses if a pilot already exists; --force clears it and redraws."""
    cfg = load_config()
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
            "WHERE r.is_selected AND h.in_final_sample ORDER BY r.review_id").fetchall()
        df = pd.DataFrame(rows, columns=SELECT_COLS)
        df = df[df.destination.map(lambda d: same_destination(d or "", destination))]
        if limit:
            df = df[df.hotel_id.isin(sorted(df.hotel_id.unique())[:limit])]
        out = draw_pilot(df, cfg["seed"], cfg["pilot_max_per_hotel"], cfg["pilot_min_id"])
        conn.execute("UPDATE reviews SET is_pilot = false, pilot_stratum = NULL WHERE is_pilot OR pilot_stratum IS NOT NULL")
        for k, g in out.groupby("pilot_stratum"):
            conn.execute("UPDATE reviews SET is_pilot = true, pilot_stratum = %s WHERE review_id = ANY(%s)",
                         (k, [int(i) for i in g.review_id]))
        conn.commit()
        path = (out_dir or ROOT / "output") / "pilot_100.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        out[EXPORT_COLS].to_csv(path, index=False)
        return f"{summarise(out, cfg['pilot_max_per_hotel'])} Wrote {path}."
    finally:
        if own:
            conn.close()
