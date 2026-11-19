import pytest

from hotel_study import reviews
from hotel_study.cells import next_batch_size
from hotel_study.google import SpendLimitReached

CFG = {"review_locale": "id", "review_window": {"start": "2023-01-01", "end": "2025-12-31"}, "batch_size": 3, "no_endpage_batch_size": 2, "ceiling": 3, "end_page": 41,
       "eligibility": {"threshold": 100}, "reviews_per_hotel": 300, "max_spend_usd": 85}
CELL = ("Bali", 4)


class World:
    """In-memory hotels + a fake run_batch. `truth[h]` = (eligible_after_pass1, eligible_after_no_endpage)."""

    def __init__(self, truth, tested=(), cell=CELL):
        self.truth, self.calls, self.spend_after, self.zero = truth, [], None, {}  # zero[h] = attempts returning no items
        self.h = {i: dict(status="not_scraped", elig=None, test=False, final=None, url=f"u{i}") for i in truth}  # keys ARE cell_order
        for i in tested:
            self.h[i].update(status="complete", elig=truth[i][1] >= 100, test=True)

    def run_batch(self, client, conn, batch, cfg, pass_, end_page):
        ids = [b[0] for b in batch]
        assert len(self.calls) < 30, "runaway loop"
        if self.spend_after is not None and len(self.calls) >= self.spend_after:
            raise SpendLimitReached("max_spend_usd 85 reached (spent 85)")
        self.calls.append((ids, pass_, end_page))
        n_set = 0
        for i in ids:
            if self.zero.get(i, 0) > 0:
                self.zero[i] -= 1
                continue
            n_set += 1
            assert self.h[i]["status"] in ("not_scraped", "needs_pass2")
            n = self.truth[i][0 if end_page else 1]
            st, el = reviews.hotel_status(n, 41 if self.truth[i][0] != self.truth[i][1] else 0, end_page, 300, 100)
            self.h[i].update(status=st, elig=el)
        return n_set  # like the real run_batch: hotels updated


class Conn:
    def __init__(self, w): self.w, self.last, self.args = w, "", ()

    def execute(self, sql, args=()):
        self.last, self.args = " ".join(sql.split()), args
        if self.last.startswith("UPDATE hotels SET scrape_status = 'not_needed'"):
            for h in self.w.h.values():
                if h["status"] == "not_scraped":
                    h["status"] = "not_needed"
        elif self.last.startswith("UPDATE hotels SET scrape_status = 'complete'"):
            for i in self.args[0]:
                self.w.h[i].update(status="complete", elig=False, zeroed=True)
        elif self.last.startswith("UPDATE hotels SET in_final_sample"):
            for i, h in self.w.h.items():
                h["final"] = i in self.args[0]
        return self

    def fetchall(self):
        if "FROM hotels" in self.last:
            return [(i, h["url"], h["status"], h["elig"], h["test"]) for i, h in sorted(self.w.h.items())]
        raise AssertionError(self.last)

    def fetchone(self): return (0,)
    def commit(self): pass


@pytest.fixture
def run(monkeypatch):
    def go(truth, tested=(), cfg=CFG, share_calls=None):
        w = World(truth, tested)
        monkeypatch.setattr(reviews, "run_batch", w.run_batch)
        reviews.scrape_cell(None, Conn(w), CELL, cfg)
        return w
    return go


E, N = (150, 150), (50, 50)


def scraped(w): return [i for c in w.calls for i in c[0]]


def test_stops_at_ceiling_and_rest_not_needed(run):
    w = run({i: E for i in range(1, 11)})
    assert scraped(w) == [1, 2, 3]
    assert all(h["status"] == "not_needed" for i, h in w.h.items() if i > 3)
    assert sum(bool(h["final"]) for h in w.h.values()) == 3 and all(w.calls[0][2] == 41 for _ in [0])


def test_first_ceiling_eligible_by_order_get_final(run):
    w = run({1: N, 2: E, 3: N, 4: E, 5: E, 6: E, 7: E}, tested=(1, 2, 3, 4, 5))  # five tested: 2,4,5 eligible -> done, 6,7 never scraped
    assert scraped(w) == [] and [i for i, h in w.h.items() if h["final"]] == [2, 4, 5]
    assert w.h[6]["status"] == w.h[7]["status"] == "not_needed"


def test_later_eligible_not_in_final(run):
    w = run({i: E for i in range(1, 6)}, tested=(1,))  # needs 2 more with share 1.0 -> batch of 2
    assert scraped(w) == [2, 3] and w.h[1]["final"] and not w.h[4]["final"]
    w2 = run({1: E, 2: E, 3: E, 4: E, 5: E}, tested=(1, 2, 3, 4))  # 4 eligible tested > ceiling 3
    assert [i for i, h in w2.h.items() if h["final"]] == [1, 2, 3] and w2.h[4]["elig"] is True and not w2.h[4]["final"]


def test_fewer_eligible_than_ceiling_scrapes_all(run):
    w = run({1: E, 2: N, 3: N, 4: N, 5: N, 6: N, 7: N}, tested=(1,))
    assert sorted(scraped(w)) == [2, 3, 4, 5, 6, 7] and not any(h["status"] == "not_needed" for h in w.h.values())
    assert [i for i, h in w.h.items() if h["final"]] == [1]


def test_tested_hotels_not_rescraped(run):
    w = run({i: E for i in range(1, 8)}, tested=(1, 2))
    assert not {1, 2} & set(scraped(w))


def test_batch_shrinks_with_test_share(run):
    # 1 of 2 tested eligible -> share .5; needed 2 -> want 4 -> capped at batch_size 3
    w = run({i: E for i in range(1, 10)} | {1: E, 2: N}, tested=(1, 2))
    assert len(w.calls[0][0]) == next_batch_size(2, 0.5, 3, 7)
    # needed 1, share 1.0 -> batch of 1
    w = run({i: E for i in range(1, 10)}, tested=(1, 2, 3)[:2])
    assert len(w.calls[0][0]) == 1


def test_zero_share_uses_max_batch(run):
    w = run({i: N for i in range(1, 8)}, tested=(1, 2, 3))
    assert len(w.calls[0][0]) == 3


def test_done_cell_never_calls_next_batch_size(run, monkeypatch):
    def boom(*a): raise AssertionError("next_batch_size called")
    monkeypatch.setattr(reviews, "next_batch_size", boom)
    w = run({i: E for i in range(1, 6)}, tested=(1, 2, 3))  # needed 0
    assert w.calls == [] and w.h[4]["status"] == "not_needed"
    w = run({1: N, 2: N, 3: N}, tested=(1, 2, 3))  # remaining 0
    assert w.calls == [] and not w.h[1]["final"]


def test_undecided_rescraped_immediately_without_endpage(run):
    # hotel 1: pass1 80 eligible with pages==end_page -> needs_pass2/None; no-endPage rerun finds 120
    truth = {1: (80, 120), 2: E, 3: E, 4: E, 5: E}
    w = run(truth)
    first_two = w.calls[:2]
    assert first_two[0][1:] == ("1", 41) and first_two[0][0][0] == 1
    assert w.calls[1] == ([1], "2", None)  # next call is the undecided rescrape, before any further pass-1 batch
    assert w.h[1]["elig"] is True


def test_spend_cap_stops_with_message(run):
    w = World({i: N for i in range(1, 10)})
    w.spend_after = 1

    def go(mp):
        mp.setattr(reviews, "run_batch", w.run_batch)
        reviews.scrape_cell(None, Conn(w), CELL, CFG)
    with pytest.MonkeyPatch.context() as mp, pytest.raises(SpendLimitReached, match="max_spend_usd"):
        go(mp)
    assert all(h["status"] != "not_needed" for h in w.h.values())  # undecided cell: not finalized


def test_rerun_after_crash_skips_scraped(monkeypatch):
    cfg = CFG | {"ceiling": 6}
    w = World({i: E for i in range(1, 9)})
    w.spend_after = 1
    monkeypatch.setattr(reviews, "run_batch", w.run_batch)
    with pytest.raises(SpendLimitReached):
        reviews.scrape_cell(None, Conn(w), CELL, cfg)
    done = list(scraped(w))
    w.spend_after = None
    reviews.scrape_cell(None, Conn(w), CELL, cfg)
    again = scraped(w)[len(done):]
    assert len(done) == 3 and not set(done) & set(again) and len(scraped(w)) == len(set(scraped(w)))
    assert sum(bool(h["final"]) for h in w.h.values()) == 6


def test_failed_run_stalls_cell(monkeypatch):
    w = World({i: E for i in range(1, 5)})
    monkeypatch.setattr(reviews, "run_batch", lambda *a: None)
    with pytest.raises(RuntimeError, match="failed"):
        reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert all(h["status"] == "not_scraped" for h in w.h.values())


def test_main_pass_requires_end_page(monkeypatch):
    monkeypatch.setattr(reviews, "load_config", lambda: CFG | {"end_page": None})
    with pytest.raises(RuntimeError, match="end_page"):
        reviews.scrape_reviews("1", False)


# ---- pass 2 ----
class P2Conn:
    def __init__(self, rows, unfinished=0): self.rows, self.unfinished, self.last = rows, unfinished, ""
    def execute(self, sql, args=()): self.last = " ".join(sql.split()); self.args = args; return self
    def commit(self): pass
    def fetchone(self): return (0,) if "scrape_runs" in self.last else ((1,) if self.unfinished else None)
    def fetchall(self):
        if "DISTINCT destination" in self.last:
            return [("Bali",), ("Jakarta",)]
        assert "in_final_sample" in self.last and "needs_pass2" in self.last
        return self.rows


def test_pass2_sends_no_endpage(monkeypatch):
    calls = []
    monkeypatch.setattr(reviews, "run_batch", lambda c, conn, b, cfg, p, e: calls.append(([x[0] for x in b], p, e)) or len(b))
    rows = [(1, "Bali", 4, "u1"), (2, "Bali", 4, "u2"), (3, "Bali", 5, "u3")]
    out = reviews.scrape_pass2(None, P2Conn(rows), CFG)
    assert calls == [([1, 2], "2", None), ([3], "2", None)] and "3 hotels" in out


def test_pass2_refuses_unfinished_cells(monkeypatch):
    monkeypatch.setattr(reviews, "run_batch", lambda *a: pytest.fail("ran"))
    with pytest.raises(RuntimeError, match="pass 1"):
        reviews.scrape_pass2(None, P2Conn([], unfinished=1), CFG)


# ---- fix round 1 ----
def _world(truth, monkeypatch, **kw):
    w = World(truth, **kw)
    monkeypatch.setattr(reviews, "run_batch", w.run_batch)
    return w


def test_undecided_no_items_raises_once(monkeypatch):
    w = _world({1: (80, 120), 2: E, 3: E, 4: E}, monkeypatch)
    orig = w.run_batch

    def rb(client, conn, batch, cfg, pass_, end_page):
        if pass_ == "2":
            w.zero[1] = 1  # the no-endPage run returns nothing for hotel 1
        return orig(client, conn, batch, cfg, pass_, end_page)
    monkeypatch.setattr(reviews, "run_batch", rb)
    with pytest.raises(RuntimeError, match=r"\[1\].*manually"):
        reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert [c for c in w.calls if c[1] == "2"] == [([1], "2", None)]  # exactly one pass-2 attempt
    assert w.h[1]["status"] == "needs_pass2" and w.h[1]["elig"] is None
    assert all(h["status"] != "not_needed" for h in w.h.values())


SQL_HINT = "update hotels set scrape_status='complete', n_raw_reviews=0, n_out_of_window=0, n_other_language=0, n_eligible_reviews=0, is_eligible=false, in_final_sample=false where hotel_id = "


def test_zero_review_hotel_marked_when_batch_mates_returned(monkeypatch):
    w = _world({i: E for i in range(1, 8)}, monkeypatch)
    w.zero = {1: 99}
    reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert sum(1 for c in w.calls if 1 in c[0]) == 2  # first attempt + one retry
    assert w.h[1]["status"] == "complete" and w.h[1]["elig"] is False and w.h[1]["zeroed"] and not w.h[1]["final"]
    assert sum(bool(h["final"]) for h in w.h.values()) == 3  # cell continued / finalised


def test_whole_batch_empty_raises_and_marks_nothing(monkeypatch):
    w = _world({i: E for i in range(1, 8)}, monkeypatch)
    w.zero = {1: 99, 2: 99, 3: 99}
    with pytest.raises(RuntimeError, match=r"\[1, 2, 3\].*ambiguous"):
        reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert not any(h.get("zeroed") for h in w.h.values()) and all(h["status"] == "not_scraped" for h in w.h.values())


def test_single_hotel_batch_empty_twice_raises_with_sql(monkeypatch):
    cfg = CFG | {"batch_size": 1}
    w = _world({1: E, 2: E}, monkeypatch)
    w.zero = {1: 99}
    with pytest.raises(RuntimeError) as e:
        reviews.scrape_cell(None, Conn(w), CELL, cfg)
    assert SQL_HINT + "1" in str(e.value) and "ambiguous" in str(e.value)
    assert not w.h[1].get("zeroed") and w.h[1]["status"] == "not_scraped"
    assert sum(1 for c in w.calls if 1 in c[0]) == 2


def test_zero_then_reviews_on_retry(monkeypatch):
    w = _world({i: E for i in range(1, 8)}, monkeypatch)
    w.zero = {2: 1}
    reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert w.h[2]["elig"] is True and not w.h[2].get("zeroed") and w.h[2]["final"]


def test_undecided_after_ceiling_not_rescraped(monkeypatch):
    # 1,2,3 eligible fill the ceiling; hotel 4 is undecided but ordered after the 3rd eligible
    w = _world({i: E for i in range(1, 6)}, monkeypatch, tested=(1, 2, 3))
    w.h[4].update(status="needs_pass2", elig=None)
    reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert w.calls == [] and w.h[4]["status"] == "needs_pass2"
    w2 = _world({1: E, 2: E, 3: E, 4: E}, monkeypatch, tested=(2, 3, 4))
    w2.h[1].update(status="needs_pass2", elig=None)
    w2.truth[1] = (80, 120)
    reviews.scrape_cell(None, Conn(w2), CELL, CFG)
    assert w2.calls == [([1], "2", None)]  # ordered before the ceiling-th eligible: must be resolved


def _entry(monkeypatch, cells, fail=(), spend=()):
    class C:
        def execute(self, sql, args=()): self.sql = sql; return self
        def fetchall(self): return cells
        def close(self): pass
    monkeypatch.setattr(reviews, "ApifyClient", lambda t: None)
    monkeypatch.setattr(reviews, "get_secret", lambda n: "x")
    monkeypatch.setattr(reviews, "load_config", lambda: CFG)
    monkeypatch.setattr(reviews, "reconcile", lambda *a: 0)
    seen = []

    def sc(client, conn, cell, cfg):
        seen.append(cell)
        if cell in fail: raise RuntimeError("stalled")
        if cell in spend: raise SpendLimitReached("max_spend_usd")
        return 3
    monkeypatch.setattr(reviews, "scrape_cell", sc)
    return seen, C()


def test_failing_cell_does_not_block_others(monkeypatch):
    seen, c = _entry(monkeypatch, [("Bali", 3), ("Bali", 4), ("Bali", 5)], fail=[("Bali", 3)])
    with pytest.raises(RuntimeError, match=r"2 cells finished.*1 failed"):
        reviews.scrape_reviews("1", False, conn=c)
    assert seen == [("Bali", 3), ("Bali", 4), ("Bali", 5)]


def test_spend_limit_stops_all_cells(monkeypatch):
    # guards the except-clause order in scrape_reviews: SpendLimitReached is a RuntimeError and must not be collected per cell
    seen, c = _entry(monkeypatch, [("Bali", 3), ("Bali", 4), ("Bali", 5)], spend=[("Bali", 4)])
    with pytest.raises(SpendLimitReached):
        reviews.scrape_reviews("1", False, conn=c)
    assert seen == [("Bali", 3), ("Bali", 4)]


def test_pass_must_be_1_or_2(monkeypatch):
    def live(*a, **k): raise AssertionError("touched a live service")
    monkeypatch.setattr(reviews, "load_config", lambda: CFG)
    monkeypatch.setattr(reviews, "ApifyClient", live)
    monkeypatch.setattr(reviews, "get_secret", live)
    monkeypatch.setattr("hotel_study.db.connect", live)
    with pytest.raises(RuntimeError, match="--pass"):
        reviews.scrape_reviews("3", False)


def test_pass2_reports_partial_failures(monkeypatch):
    monkeypatch.setattr(reviews, "run_batch", lambda c, conn, b, cfg, p, e: len(b) - 1)
    out = reviews.scrape_pass2(None, P2Conn([(1, "Bali", 4, "u1"), (2, "Bali", 4, "u2")]), CFG)
    assert "1 hotels scraped, 1 failed" in out


def test_pass2_guard_query_scoped(monkeypatch):
    c = P2Conn([], unfinished=1)
    with pytest.raises(RuntimeError, match="pass 1"):
        reviews.scrape_pass2(None, c, CFG, destination="Bali")
    assert c.args[0] == ["Bali"] and "is_eligible IS NULL" in c.last


def test_update_hotels_writes_final_sample_only_for_eligibility_test(monkeypatch):
    seen = []

    class C:
        def execute(self, sql, args=()): seen.append((" ".join(sql.split()), args)); return self
        def commit(self): pass
    monkeypatch.setattr(reviews, "ingest_items", lambda *a: {1: dict(n_raw=150, n_out_of_window=0, n_other_language=0, n_eligible=150, n_bad_date=0)})
    reviews._update_hotels(C(), "run", [item(1, 1)], [1], CFG, 41)
    sql, args = seen[0]
    assert "in_final_sample = CASE WHEN in_eligibility_test THEN %s ELSE in_final_sample END" in sql
    assert args[-2] is True and args[-1] == 1  # main-pass run: the value only lands on eligibility-test rows (SQL), others untouched


def item(rid, hid, page=1):
    return {"id": rid, "location": {"locationId": hid}, "inputSource": {"pageNumber": page}}


def test_earlier_not_scraped_hotel_scraped_before_cell_closes(monkeypatch):
    # hotels 2,3,4 (eligibility test) already reach the ceiling; hotel 1 is earlier in cell_order and still not_scraped
    w = _world({i: E for i in range(1, 7)}, monkeypatch, tested=(2, 3, 4))
    reviews.scrape_cell(None, Conn(w), CELL, CFG)
    assert w.calls == [([1], "1", 41)] and w.h[1]["status"] == "complete"
    assert [i for i, h in w.h.items() if h["final"]] == [1, 2, 3] and w.h[5]["status"] == w.h[6]["status"] == "not_needed"


def test_undecided_batches_capped_by_no_endpage_batch_size(monkeypatch):
    w = _world({i: (80, 120) for i in range(1, 6)}, monkeypatch)
    for i in (1, 2, 3):
        w.h[i].update(status="needs_pass2", elig=None)
    reviews.scrape_cell(None, Conn(w), CELL, CFG | {"ceiling": 2})
    p2 = [c for c in w.calls if c[1] == "2"]
    assert p2 and all(len(c[0]) <= 2 and c[2] is None for c in p2)


def test_pass2_batches_capped_by_no_endpage_batch_size(monkeypatch):
    calls = []
    monkeypatch.setattr(reviews, "run_batch", lambda c, conn, b, cfg, p, e: calls.append(len(b)) or len(b))
    rows = [(i, "Bali", 4, f"u{i}") for i in range(1, 6)]
    reviews.scrape_pass2(None, P2Conn(rows), CFG)
    assert calls == [2, 2, 1]
