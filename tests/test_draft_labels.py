import json

import pandas as pd
import pytest

from hotel_study.draft_labels import cost_usd, draw_sample, parse_phrases, stats


def test_parse_phrases_ok_and_bad():
    ok = json.dumps({"phrases": [{"phrase": "kamar bersih", "label": "TAN", "sentiment": "positive", "flag": None}]})
    assert parse_phrases(ok)[0]["label"] == "TAN"
    with pytest.raises(ValueError):
        parse_phrases(json.dumps({"phrases": [{"phrase": "x", "label": "XXX", "sentiment": "positive"}]}))


def test_draw_sample_deterministic_and_order_free():
    ids = list(range(1000))
    assert draw_sample(ids, 50, 42) == draw_sample(ids[::-1], 50, 42) and len(draw_sample(ids, 5000, 42)) == 1000


def test_cost_and_stats():
    assert cost_usd(1_000_000, 1_000_000, {"input": 0.1, "output": 0.5}) == pytest.approx(0.6)
    df = pd.DataFrame({"language": ["id", "en", "en"], "label": ["EMP"] * 3, "sentiment": ["negative", "negative", "positive"]})
    s = stats(df)
    assert s[(s.language == "all") & (s.sentiment == "negative")].n.item() == 2
