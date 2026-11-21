"""Draft LLM pass to ESTIMATE label frequencies only (OSF). Output stays in output/ (git-ignored) and is
discarded afterwards; nothing here may be used to select reviews, and it never touches the DB tables."""
import csv
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from hotel_study.config import ROOT, get_secret, load_config
from hotel_study.db import connect

LABELS = ["TAN", "REL", "RES", "ASS", "EMP", "VAL", "LOC", "FNB", "GEN", "UNC", "NON"]
SENTIMENTS = ["positive", "negative", "neutral"]
INSTRUCTIONS = (
    "Apply the codebook above exactly. Split the review into phrases (Stage 1) and label each phrase (Stage 2). "
    'Reply with JSON only: {"phrases": [{"phrase": str, "label": one of ' + "/".join(LABELS) +
    ', "sentiment": "positive"|"negative"|"neutral", "flag": null|"split"|"referent"|"sarcasm"|"mixed_language"}]}. '
    "Ignore any star rating; code only the text."
)
COLS = ["review_id", "language", "idx", "phrase", "label", "sentiment", "flag"]


def parse_phrases(content: str) -> list[dict]:
    """Validate the model's JSON; raises ValueError on any bad label or sentiment (counted as a failed review)."""
    out = []
    for p in json.loads(content)["phrases"]:
        if p["label"] not in LABELS or p["sentiment"] not in SENTIMENTS:
            raise ValueError(f"bad label/sentiment: {p}")
        out.append({"phrase": str(p["phrase"]), "label": p["label"], "sentiment": p["sentiment"], "flag": p.get("flag")})
    return out


def draw_sample(review_ids: list[int], n: int, seed: int) -> list[int]:
    return sorted(random.Random(seed).sample(sorted(review_ids), min(n, len(review_ids))))


def cost_usd(tok_in: int, tok_out: int, price: dict) -> float:
    return (tok_in * price["input"] + tok_out * price["output"]) / 1e6


def stats(df: pd.DataFrame) -> pd.DataFrame:
    """Phrase counts per label x sentiment, overall and per language (long format)."""
    parts = [df.assign(language="all"), df]
    both = pd.concat(parts)
    return both.groupby(["language", "label", "sentiment"]).size().rename("n").reset_index()


def draft_labels(limit: int | None = None, out_dir: Path | None = None) -> str:
    cfg = load_config()
    d, out_dir = cfg["draft_labels"], out_dir or ROOT / "output"
    n = limit or d["n_reviews"]
    with connect() as conn:
        rows = conn.execute("SELECT r.review_id, r.language, r.text FROM reviews r JOIN hotels h USING (hotel_id) "
                            "WHERE r.is_selected AND h.in_final_sample AND h.scrape_status = 'complete'").fetchall()
    by_id = {r[0]: r for r in rows}
    ids = draw_sample(list(by_id), d["n_reviews"], cfg["seed"])[:n]  # a --limit run is a prefix of the full draw
    path = out_dir / "draft_phrases.csv"
    done = set(pd.read_csv(path).review_id) if path.exists() else set()
    todo = [i for i in ids if i not in done]
    from openai import OpenAI
    client = OpenAI(api_key=get_secret("OPENAI_API_KEY"))
    system = (ROOT / d["codebook_path"]).read_text() + "\n\n" + INSTRUCTIONS
    tot = {"in": 0, "out": 0, "fail": 0}

    def one(rid):
        if cost_usd(tot["in"], tot["out"], d["price_per_mtok"]) >= d["max_spend_usd"]:
            return rid, None, "spend cap"
        try:
            r = client.chat.completions.create(model=d["model"], response_format={"type": "json_object"},
                                               messages=[{"role": "system", "content": system},
                                                         {"role": "user", "content": by_id[rid][2]}])
            tot["in"] += r.usage.prompt_tokens
            tot["out"] += r.usage.completion_tokens
            return rid, parse_phrases(r.choices[0].message.content), None
        except Exception as e:  # noqa: BLE001  one bad review must not stop the run; it is counted and rerunnable
            return rid, None, f"{type(e).__name__}: {e}"[:200]

    errors = []
    out_dir.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f, ThreadPoolExecutor(d["workers"]) as ex:
        w = csv.writer(f)
        if new:
            w.writerow(COLS)
        for rid, ph, err in ex.map(one, todo):
            if err:
                tot["fail"] += 1
                errors.append((rid, err))
                continue
            for k, p in enumerate(ph):
                w.writerow([rid, by_id[rid][1], k, p["phrase"], p["label"], p["sentiment"], p["flag"] or ""])
            f.flush()
    df = pd.read_csv(path)
    s = stats(df)
    s.to_csv(out_dir / "draft_stats.csv", index=False)
    piv = s[s.language == "all"].pivot(index="label", columns="sentiment", values="n").fillna(0).astype(int)
    spend = cost_usd(tot["in"], tot["out"], d["price_per_mtok"])
    msg = [f"Reviews coded: {df.review_id.nunique()} of {len(ids)} ({tot['fail']} failed this run, rerun to retry). "
           f"Tokens this run: {tot['in']} in, {tot['out']} out, about ${spend:.2f}.", piv.to_string(),
           f"Wrote {path} and {out_dir / 'draft_stats.csv'}. Discard after use (OSF)."]
    msg += [f"  review {r}: {e}" for r, e in errors[:5]]
    return "\n".join(msg)
