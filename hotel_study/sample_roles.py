"""Assign each selected review a role: pilot (existing), training, gold (dev_set/test_set) or none. Seed 42.
Strata never use LLM labels (OSF). Order: pilot kept, then training, then gold from what is left."""
import random

import pandas as pd

from hotel_study.config import load_config
from hotel_study.db import connect
from hotel_study.pilot import allocate


def band(rating: int, bands: dict) -> str:
    return next(k for k, (lo, hi) in bands.items() if lo <= rating <= hi)


def _split_dev(strata_sizes: dict, dev_total: int) -> dict:
    """Dev count per stratum, proportional to size, largest remainder; sums to dev_total."""
    n = sum(strata_sizes.values())
    base = {k: v * dev_total // n for k, v in strata_sizes.items()}
    rest = dev_total - sum(base.values())
    for k in sorted(strata_sizes, key=lambda k: (-(strata_sizes[k] * dev_total % n), str(k)))[:rest]:
        base[k] += 1
    return base


def draw_roles(df: pd.DataFrame, cfg: dict) -> dict:
    """df columns: review_id, language, final_star_rating, rating (non-pilot selected reviews).
    Returns {review_id: 'training'|'dev_set'|'test_set'}."""
    sr, rng = cfg["sample_roles"], random.Random(cfg["seed"])
    rows = df.sort_values("review_id").to_dict("records")
    for r in rows:
        r["band"] = band(r["rating"], sr["rating_bands"])
    out, used = {}, set()
    # training: half per language, spread equally over star classes
    by = {}
    for r in rows:
        by.setdefault((r["language"], r["final_star_rating"]), []).append(r)
    for lang, total in (("id", sr["training_size"] // 2), ("en", sr["training_size"] - sr["training_size"] // 2)):
        keys = sorted(k for k in by if k[0] == lang)
        quota = allocate({k: len(by[k]) for k in keys}, total)
        for k in keys:
            for r in rng.sample(by[k], quota[k]):
                out[r["review_id"]] = "training"
                used.add(r["review_id"])
    # gold: strata language x star x band; ID capped by availability, English fills the rest
    strata = {}
    for r in rows:
        if r["review_id"] not in used:
            strata.setdefault((r["language"], r["final_star_rating"], r["band"]), []).append(r)
    half = sr["gold_size"] // 2
    quota = allocate({k: len(v) for k, v in strata.items() if k[0] == "id"}, half)
    n_id = sum(quota.values())
    quota |= allocate({k: len(v) for k, v in strata.items() if k[0] == "en"}, sr["gold_size"] - n_id)
    picked = {k: rng.sample(strata[k], q) for k, q in sorted(quota.items())}
    dev = _split_dev({k: len(v) for k, v in picked.items()}, sr["dev_size"])
    for k, rs in picked.items():
        rng.shuffle(rs)
        for i, r in enumerate(rs):
            out[r["review_id"]] = "dev_set" if i < dev[k] else "test_set"
    return out


def assign_sample_roles(force: bool = False, dry_run: bool = False) -> str:
    cfg = load_config()
    with connect() as conn:
        if not dry_run and not force and conn.execute(
                "SELECT 1 FROM reviews WHERE is_training OR gold_set IS NOT NULL LIMIT 1").fetchone():
            raise RuntimeError("assign-sample-roles refused: roles already assigned; --force redraws them")
        rows = conn.execute(
            "SELECT r.review_id, r.language, h.final_star_rating, r.rating FROM reviews r JOIN hotels h USING (hotel_id) "
            "WHERE r.is_selected AND h.in_final_sample AND h.scrape_status = 'complete' AND NOT r.is_pilot "
            "AND r.rating IS NOT NULL").fetchall()
        df = pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating", "rating"])
        roles = draw_roles(df, cfg)
        meta = df.set_index("review_id").assign(role=pd.Series(roles)).dropna(subset=["role"])
        meta["band"] = meta.rating.map(lambda x: band(x, cfg["sample_roles"]["rating_bands"]))
        lines = [f"Pool (non-pilot selected): {len(df)}. Assigned: " + ", ".join(f"{k} {v}" for k, v in meta.role.value_counts().items()),
                 "Gold by language/role:\n" + pd.crosstab(meta[meta.role != "training"].language, meta.role).to_string(),
                 "Gold by star class x band:\n" + pd.crosstab([meta.final_star_rating, meta.band], [meta.language]).to_string()]
        g = meta[meta.role != "training"]
        lines.append("Gold by language x star x band x role (min cell): " + str(g.groupby(["language", "final_star_rating", "band", "role"]).size().min()))
        if (g[(g.role == "test_set") & (g.language == "id")]).shape[0] < 30:
            lines.append("WARNING: fewer than 30 Indonesian reviews in the test set (registered language contingency).")
        if dry_run:
            return "DRY RUN (nothing written)\n" + "\n".join(lines)
        conn.execute("UPDATE reviews SET is_training = false, gold_set = NULL WHERE is_training OR gold_set IS NOT NULL")
        for role, col in (("training", None), ("dev_set", "dev_set"), ("test_set", "test_set")):
            ids = [int(i) for i in meta.index[meta.role == role]]
            if role == "training":
                conn.execute("UPDATE reviews SET is_training = true WHERE review_id = ANY(%s)", (ids,))
            else:
                conn.execute("UPDATE reviews SET gold_set = %s WHERE review_id = ANY(%s)", (col, ids))
        conn.commit()
        return "\n".join(lines)
