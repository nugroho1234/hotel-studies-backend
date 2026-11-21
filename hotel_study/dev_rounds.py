"""Fresh development rounds. If human kappa misses .70 the registration asks for a fresh batch of never-used reviews.
`plan-dev-rounds` pre-draws the reserve for the next rounds (same recipe as the first draw, seed + round number); the lead's
"Draw fresh dev set" button (SQL draw_dev_round) only switches to it, after a newer codebook version exists."""
import random

import pandas as pd
import yaml

from hotel_study.config import ROOT, load_config
from hotel_study.db import connect
from hotel_study.pilot import allocate
from hotel_study.sample_roles import band
from hotel_study.supp_rel import SUPP, kw_regex, load_keywords, matches


def draw_main(df: pd.DataFrame, seed: int, size: int, bands: dict) -> list[int]:
    """df: review_id, language, final_star_rating, rating. Strata language x star x rating band; half Indonesian (capped by
    availability), English fills the rest, as in the first draw."""
    rng, strata = random.Random(seed), {}
    for r in df.sort_values("review_id").to_dict("records"):
        strata.setdefault((r["language"], r["final_star_rating"], band(r["rating"], bands)), []).append(r["review_id"])
    quota = allocate({k: len(v) for k, v in strata.items() if k[0] == "id"}, size // 2)
    quota |= allocate({k: len(v) for k, v in strata.items() if k[0] == "en"}, size - sum(quota.values()))
    return [i for k, q in sorted(quota.items()) for i in rng.sample(strata[k], q)]


def plan_rounds(rounds: int = 1, force: bool = False) -> str:
    from hotel_study.supp_rel import draw_supp

    cfg = load_config()
    sr = cfg["sample_roles"]
    sup = yaml.safe_load((ROOT / "config" / "rel_keywords.yaml").read_text())["supplement"]
    pats = {lang: [(k, kw_regex(k)) for k in ks] for lang, ks in load_keywords().items()}
    with connect() as conn:
        cur = conn.execute("SELECT max(round) FROM dev_rounds").fetchone()[0] or 1
        if force:   # redraw reserves that have not been used yet
            conn.execute("DELETE FROM dev_round_reserve WHERE round > %s", (cur,))
        have = {r[0] for r in conn.execute("SELECT DISTINCT round FROM dev_round_reserve").fetchall()}
        rows = conn.execute(
            "SELECT r.review_id, r.language, h.final_star_rating, r.rating, r.text FROM reviews r JOIN hotels h USING (hotel_id) "
            "WHERE r.is_selected AND h.in_final_sample AND h.scrape_status = 'complete' AND NOT r.is_pilot AND NOT r.is_training "
            "AND r.gold_set IS NULL AND r.rating IS NOT NULL AND r.review_id NOT IN (SELECT review_id FROM dev_round_reserve)").fetchall()
        pool = pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating", "rating", "text"])
        lines = [f"Never-used pool: {len(pool)} reviews."]
        for rnd in range(cur + 1, cur + rounds + 1):
            if rnd in have:
                lines.append(f"Round {rnd}: reserve already drawn (use --force to redraw).")
                continue
            seed = cfg["seed"] + rnd
            main = draw_main(pool, seed, sr["dev_size"], sr["rating_bands"])
            rest = pool[~pool.review_id.isin(main)]
            rest = rest[[bool(matches(t, pats[lg])) for t, lg in zip(rest.text, rest.language)]]
            supp, info = draw_supp(rest, seed, sup["dev_size"], sup["dev_size"])
            for i in main:
                conn.execute("INSERT INTO dev_round_reserve (round, review_id, role) VALUES (%s, %s, 'dev_set')", (rnd, int(i)))
            for i, role in supp.items():
                conn.execute("INSERT INTO dev_round_reserve (round, review_id, role) VALUES (%s, %s, %s)", (rnd, int(i), role))
            assert set(supp.values()) <= {SUPP[0]}
            used = set(main) | set(supp)
            pool = pool[~pool.review_id.isin(used)]
            sel = pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating", "rating", "text"]).set_index("review_id").loc[sorted(used)]
            lines.append(f"Round {rnd} (seed {seed}): {len(main)} main + {len(supp)} supplement; " + ", ".join(f"{k} {v}" for k, v in sel.language.value_counts().items()))
            if len(main) < sr["dev_size"] or len(supp) < sup["dev_size"]:
                lines.append(f"  SHORT: wanted {sr['dev_size']} + {sup['dev_size']}.")
        conn.commit()
        return "\n".join(lines)
