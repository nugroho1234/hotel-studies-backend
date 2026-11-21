import pandas as pd

from hotel_study.dev_rounds import draw_main

BANDS = {"low": [1, 2], "mid": [3, 3], "high": [4, 5]}


def pool():
    rows = [(i, "id" if i % 2 else "en", 3 + i % 3, 1 + i % 5) for i in range(1, 1200)]
    return pd.DataFrame(rows, columns=["review_id", "language", "final_star_rating", "rating"])


def test_draw_main_size_language_and_seed():
    a = draw_main(pool(), 43, 70, BANDS)
    assert len(a) == len(set(a)) == 70
    assert sum(1 for i in a if i % 2) == 35
    assert a == draw_main(pool(), 43, 70, BANDS)
    assert set(a) != set(draw_main(pool(), 44, 70, BANDS))
