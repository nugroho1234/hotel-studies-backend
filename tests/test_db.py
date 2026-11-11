import pytest

from hotel_study.config import get_secret
from hotel_study.db import connect, upsert

try:
    get_secret("SUPABASE_DB_URL")
    HAVE_DB = True
except RuntimeError:
    HAVE_DB = False

pytestmark = pytest.mark.skipif(not HAVE_DB, reason="SUPABASE_DB_URL not set")


def test_upsert_twice_creates_one_row():
    row = {"hotel_id": 999999999001, "hotel_name": "T", "destination": "X", "star_class": 3,
           "review_url": "https://example.invalid/-d999999999001-"}
    with connect() as conn:  # leaving the block without commit rolls back
        upsert(conn, "hotels", [row], "hotel_id")
        upsert(conn, "hotels", [{**row, "hotel_name": "T2"}], "hotel_id")
        n, name = conn.execute("select count(*), max(hotel_name) from hotels where hotel_id=%s",
                               (row["hotel_id"],)).fetchone()
        conn.rollback()
    assert (n, name) == (1, "T2")


def test_upsert_jsonb_dict_roundtrips():
    inp = {"startUrls": [{"url": "u"}], "since": "2023-01-01"}
    with connect() as conn:
        upsert(conn, "scrape_runs", [{"run_id": "test-run-jsonb", "actor": "a", "pass": "1", "input": inp}], "run_id")
        got = conn.execute("select input from scrape_runs where run_id='test-run-jsonb'").fetchone()[0]
        conn.rollback()
    assert got == inp
