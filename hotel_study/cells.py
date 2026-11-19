import math
import random
from itertools import groupby

from hotel_study.cli import same_destination
from hotel_study.config import load_config
from hotel_study.db import connect


def assign_order(hotel_ids: list[int], seed: int) -> dict[int, int]:
    """Seeded random permutation, 1-based. Ids are sorted first so input order never matters."""
    ids = sorted(hotel_ids)
    random.Random(seed).shuffle(ids)
    return {h: i for i, h in enumerate(ids, 1)}


def cell_done(n_eligible: int, ceiling: int) -> bool:
    return n_eligible >= ceiling


def next_batch_size(needed: int, test_share: float, max_batch: int, remaining: int) -> int:
    """Hotels to scrape next: enough to expect `needed` eligible, capped by max_batch and remaining."""
    want = math.ceil(needed / test_share) if test_share > 0 else max_batch
    return max(1, min(max_batch, want, remaining))


def order_cells(limit: int | None = None, destination: str | None = None, conn=None) -> str:
    """Assign cell_order per (destination, final_star_rating). Refuses once any review exists."""
    cfg = load_config()
    own = conn is None
    conn = conn or connect()
    try:
        if conn.execute("SELECT 1 FROM reviews LIMIT 1").fetchone():
            raise RuntimeError("order-cells refused: reviews already exist; the order is fixed once scraping has started")
        rows = conn.execute(
            "SELECT hotel_id, destination, final_star_rating FROM hotels "
            "WHERE in_sample_frame AND num_reviews_ge_100 ORDER BY destination, final_star_rating, hotel_id").fetchall()
        rows = [r for r in rows if same_destination(r[1] or "", destination)]
        cells = [(k, [r[0] for r in g]) for k, g in groupby(rows, key=lambda r: (r[1], r[2]))]
        cells = cells[:limit]
        for (dest, star), ids in cells:
            # NULL first so reassigning never trips the unique index mid-transaction.
            conn.execute("UPDATE hotels SET cell_order = NULL, in_eligibility_test = false "
                         "WHERE destination = %s AND final_star_rating = %s", (dest, star))
            for h, n in assign_order(ids, cfg["seed"]).items():
                conn.execute("UPDATE hotels SET cell_order = %s, in_eligibility_test = %s WHERE hotel_id = %s",
                             (n, n <= cfg["eligibility_test_size"], h))
        conn.commit()
        return f"Ordered {len(cells)} cells, {sum(len(i) for _, i in cells)} hotels."
    finally:
        if own:
            conn.close()
