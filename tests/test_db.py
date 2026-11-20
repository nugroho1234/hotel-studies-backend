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


def _apply_schema_in_txn(conn):
    from hotel_study.db import SCHEMA
    conn.execute(SCHEMA.read_text())


def test_schema_applies_twice_on_populated_db():
    # DDL is transactional in Postgres, so this never changes the live DB (rolled back at the end)
    with connect() as conn:
        n0 = conn.execute("select count(*) from hotels").fetchone()[0]
        _apply_schema_in_txn(conn)
        _apply_schema_in_txn(conn)
        assert conn.execute("select count(*) from hotels").fetchone()[0] == n0
        conn.rollback()


def test_in_paper1_sample_follows_is_selected_and_view_status():
    base = {"hotel_name": "T", "destination": "X", "star_class": 3}
    with connect() as conn:
        _apply_schema_in_txn(conn)
        rows = [  # (hotel_id, extra columns, expected eligibility_status)
            (999999999101, {"exclusion_reason": "closed"}, "excluded"),
            (999999999102, {"scrape_status": "not_needed"}, "not_needed"),
            (999999999103, {"is_eligible": True}, "eligible"),
            (999999999104, {"is_eligible": False}, "ineligible"),
            (999999999105, {}, "pending")]
        for i, x, _ in rows:  # one call per row: upsert takes its columns from the first row
            upsert(conn, "hotels", [{**base, "hotel_id": i, "review_url": f"https://example.invalid/-d{i}-", **x}], "hotel_id")
        for i, _, want in rows:
            assert conn.execute("select eligibility_status from hotels_v where hotel_id=%s", (i,)).fetchone()[0] == want
        for rid, sel in ((999999999201, True), (999999999202, False)):
            upsert(conn, "reviews", [{"review_id": rid, "hotel_id": 999999999103, "published_date": "2024-01-01", "is_selected": sel}], "review_id")
        got = conn.execute("select review_id, in_paper1_sample from reviews where review_id in (999999999201, 999999999202) order by 1").fetchall()
        conn.rollback()
    assert got == [(999999999201, True), (999999999202, False)]
