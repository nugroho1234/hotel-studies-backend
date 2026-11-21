import random

import pandas as pd

from hotel_study.config import load_config
from hotel_study.sample_roles import _split_dev, band, draw_roles


def _df(n=6000, id_low=0.5):
    rng = random.Random(1)
    rows = []
    for i in range(n):
        lang = "id" if rng.random() < 0.5 else "en"
        rating = rng.choice([1, 2]) if rng.random() < (0.01 if lang == "id" else 0.1) else rng.choice([3, 4, 5, 5, 5])
        rows.append((i, lang, rng.choice([3, 4, 5]), rating))
    return pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating", "rating"])


def test_band():
    b = load_config()["sample_roles"]["rating_bands"]
    assert [band(r, b) for r in (1, 2, 3, 4, 5)] == ["low", "low", "mid", "high", "high"]


def test_split_dev_exact():
    assert sum(_split_dev({"a": 11, "b": 11, "c": 12, "d": 7}, 70).values()) == 70
    s = _split_dev({k: 11 for k in "abcdefghijklmnopqr"}, 70)
    assert sum(s.values()) == 70 and set(s.values()) <= {3, 4}


def test_draw_counts_exclusive_deterministic():
    cfg, df = load_config(), _df()
    a = draw_roles(df, cfg)
    assert a == draw_roles(df.sample(frac=1, random_state=3), cfg)
    v = pd.Series(a).value_counts()
    assert v["training"] == 30 and v["dev_set"] == 70 and v["test_set"] == 130
    m = df.set_index("review_id").loc[list(a)].assign(role=pd.Series(a))
    assert (m[m.role == "training"].language.value_counts() == 15).all()
    assert (m[m.role != "training"].language.value_counts() == 100).all()


def test_indonesian_shortfall_filled_by_english():
    cfg = load_config()
    df = _df()
    id_keep = df[df.language == "id"].head(60)  # only 60 Indonesian reviews exist
    df = pd.concat([df[df.language == "en"], id_keep])
    a = draw_roles(df, cfg)
    m = df.set_index("review_id").loc[list(a)].assign(role=pd.Series(a))
    g = m[m.role != "training"]
    assert len(g) == 200 and (g.language == "id").sum() <= 60
