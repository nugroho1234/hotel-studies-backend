"""Reliability oversample: reviews whose text matches the owner's keyword list (config/rel_keywords.yaml), added to the
gold standard as supp_rel_dev / supp_rel_test. Seed 42. Pool excludes pilot, training and existing gold reviews."""
import random
import re

import pandas as pd
import yaml

from hotel_study.config import ROOT, load_config
from hotel_study.db import connect
from hotel_study.pilot import allocate
from hotel_study.sample_roles import _split_dev

SUPP = ("supp_rel_dev", "supp_rel_test")


def kw_regex(kw: str) -> re.Pattern:
    """`stem*` matches words starting with stem; otherwise the whole word/phrase. Case-insensitive."""
    star = kw.endswith("*")
    body = r"\s+".join(re.escape(w) for w in kw.rstrip("*").split())
    return re.compile(r"\b" + body + (r"\w*" if star else r"\b"), re.I)


def load_keywords() -> dict[str, list[str]]:
    y = yaml.safe_load((ROOT / "config" / "rel_keywords.yaml").read_text())
    return {lang: [k for fam in y[lang].values() for k in fam] for lang in ("en", "id")}


def matches(text: str, pats: list[tuple[str, re.Pattern]]) -> list[str]:
    return [k for k, p in pats if p.search(text)]


def draw_supp(df: pd.DataFrame, seed: int, size: int, dev_size: int) -> tuple[dict, dict]:
    """df: review_id, language, final_star_rating (matching reviews only). Returns ({review_id: role}, info)."""
    rng = random.Random(seed)
    strata: dict = {}
    for r in df.sort_values("review_id").to_dict("records"):
        strata.setdefault((r["language"], r["final_star_rating"]), []).append(r)
    half = size // 2
    quota = allocate({k: len(v) for k, v in strata.items() if k[0] == "id"}, half)
    n_id = sum(quota.values())
    quota |= allocate({k: len(v) for k, v in strata.items() if k[0] == "en"}, size - n_id)
    picked = {k: rng.sample(strata[k], q) for k, q in sorted(quota.items()) if q}
    total = sum(len(v) for v in picked.values())
    dev = _split_dev({k: len(v) for k, v in picked.items()}, round(dev_size * total / size))
    out = {}
    for k, rs in picked.items():
        rng.shuffle(rs)
        for i, r in enumerate(rs):
            out[r["review_id"]] = SUPP[0] if i < dev[k] else SUPP[1]
    return out, {"total": total, "id": n_id}


def assign_supp_rel(force: bool = False, dry_run: bool = False) -> str:
    cfg, kw = load_config(), load_keywords()
    sup = yaml.safe_load((ROOT / "config" / "rel_keywords.yaml").read_text())["supplement"]
    pats = {lang: [(k, kw_regex(k)) for k in ks] for lang, ks in kw.items()}
    with connect() as conn:
        if not dry_run and not force and conn.execute("SELECT 1 FROM reviews WHERE gold_set = ANY(%s) LIMIT 1", (list(SUPP),)).fetchone():
            raise RuntimeError("assign-supp-rel refused: a supplement already exists; --force redraws it")
        rows = conn.execute(
            "SELECT r.review_id, r.language, h.final_star_rating, r.text FROM reviews r JOIN hotels h USING (hotel_id) "
            "WHERE r.is_selected AND h.in_final_sample AND h.scrape_status = 'complete' AND NOT r.is_pilot "
            "AND NOT r.is_training AND (r.gold_set IS NULL OR r.gold_set = ANY(%s))", (list(SUPP),)).fetchall()
        df = pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating", "text"])
        df["matched"] = [matches(t, pats[lg]) for t, lg in zip(df.text, df.language)]
        hit = df[df.matched.map(bool)]
        roles, info = draw_supp(hit, cfg["seed"], sup["size"], sup["dev_size"])
        per_kw = {lang: {k: int(df[df.language == lang].text.str.contains(p).sum()) for k, p in pats[lang]} for lang in pats}
        chosen = hit[hit.review_id.isin(roles)].assign(role=lambda x: x.review_id.map(roles))
        lines = [f"Pool {len(df)} reviews; matching at least one keyword: " + ", ".join(f"{lg} {n}" for lg, n in hit.language.value_counts().items()),
                 f"Drawn {info['total']} of {sup['size']} (Indonesian {info['id']}, English {info['total'] - info['id']}); "
                 + ", ".join(f"{k} {v}" for k, v in chosen.role.value_counts().items()),
                 "By language x role:\n" + pd.crosstab(chosen.language, chosen.role).to_string(),
                 "By star class x language:\n" + pd.crosstab(chosen.final_star_rating, chosen.language).to_string(),
                 "Keywords matching nothing in the pool: " + (", ".join(f"{k} ({lg})" for lg in per_kw for k, n in per_kw[lg].items() if n == 0) or "none")]
        if info["total"] < sup["size"]:
            lines.insert(0, f"SHORT: only {info['total']} of {sup['size']} reviews could be drawn.")
        if info["id"] < sup["size"] // 2:
            lines.insert(0, f"NOTE: Indonesian reached {info['id']} of {sup['size'] // 2}; English filled the rest.")
        pd.DataFrame([(lg, k, n) for lg in per_kw for k, n in per_kw[lg].items()], columns=["language", "keyword", "n_reviews"]).to_csv(
            ROOT / "output" / "supp_rel_keyword_counts.csv", index=False)
        chosen.assign(matched=chosen.matched.map("; ".join))[["review_id", "language", "final_star_rating", "role", "matched"]].to_csv(
            ROOT / "output" / "supp_rel_sample.csv", index=False)
        if dry_run:
            return "DRY RUN (nothing written)\n" + "\n".join(lines)
        conn.execute("UPDATE reviews SET gold_set = NULL WHERE gold_set = ANY(%s)", (list(SUPP),))
        for role in SUPP:
            conn.execute("UPDATE reviews SET gold_set = %s WHERE review_id = ANY(%s)",
                         (role, [int(i) for i in chosen.review_id[chosen.role == role]]))
        conn.commit()
        return "\n".join(lines)
