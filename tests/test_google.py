import pytest

from hotel_study import google
from hotel_study.google import group_results


def test_group_results_by_search_query():
    items = [{"search_query": "a", "name": "1"}, {"search_query": "b", "name": "2"}, {"search_query": "a", "name": "3"}]
    g = group_results(items)
    assert [i["name"] for i in g["a"]] == ["1", "3"] and len(g["b"]) == 1


CFG = {"google": {"top_results": 3, "hl": "en", "gl": "id", "fetch_details": False, "query_prefix": "hotels in ", "query_forms": ["plain", "name_only", "prefixed"],
                  "usage_poll_seconds": 1, "usage_max_wait_seconds": 3, "usage_zero_item_wait_seconds": 1},
       "name_match": {"accept": 90, "uncertain": 75}, "delete_remote_datasets": True, "max_spend_usd": 85}


class R:
    def __init__(self, status="SUCCEEDED", usage=None):
        self.id, self.default_dataset_id, self.status, self.usage_total_usd = "r1", "d1", status, usage


class FakeClient:
    def __init__(self, items, usages, status="SUCCEEDED", events=None):
        # items: one list for every run, or a list of lists via runs=
        self.items, self.usages, self.status, self.events = items, list(usages), status, events if events is not None else []
        self.inputs, self.gets = [], 0

    per_run = None

    def actor(self, _):
        return self

    def start(self, run_input):
        self.run_input = run_input
        self.inputs.append(run_input)
        if self.per_run:
            self.items = self.per_run.pop(0)
        return R()

    def run(self, _):
        return self

    statuses = None

    def wait_for_finish(self):
        if self.statuses:
            self.status = self.statuses.pop(0)
        return R(self.status, 0.0)

    def get(self):
        self.gets += 1
        return R(self.status, self.usages.pop(0) if len(self.usages) > 1 else self.usages[0])

    def dataset(self, _):
        return self

    def iterate_items(self):
        return iter(self.items)

    def delete(self):
        self.events.append("delete")


class Conn:
    def __init__(self, events=None):
        self.events = events if events is not None else []

    def commit(self):
        self.events.append("commit")


ROWS = []  # rows passed to upsert_rows, cleared per test


@pytest.fixture
def db(monkeypatch):
    ROWS.clear()
    events, runs = [], []
    monkeypatch.setattr(google, "upsert", lambda conn, t, rows, k: runs.append(rows[0]))
    monkeypatch.setattr(google, "upsert_rows", lambda conn, rows: (events.append("hotels"), ROWS.extend(rows)))
    monkeypatch.setattr(google, "check_spend", lambda conn, cfg: events.append("spend"))
    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    return events, runs


HOTELS = [(1, "Amankila", "bali")]
ITEM = {"search_query": "Amankila Bali", "name": "Amankila", "star_rating": 5}


def run(items, usages, hotels=HOTELS, cfg=CFG, per_run=None, **kw):
    events = kw.pop("events")
    c = FakeClient(items, usages, events=events, **kw)
    c.per_run = per_run
    return c, google.process_chunk(c, Conn(events), hotels, cfg)


def test_usage_poll_succeeds_after_retries(db):
    events, runs = db
    _, n = run([ITEM], [0.0, 0.0, 0.009], events=events)
    assert n == 1 and runs[-1]["usage_usd"] == 0.009


def test_items_but_usage_unconfirmed_raises_and_leaves_null(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="usage"):
        run([ITEM], [0.0], events=events)
    assert "usage_usd" not in runs[-1] and "hotels" not in events


def test_failed_run_unstamped_records_observed_usage(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="FAILED"):
        run([], [0.0], status="FAILED", events=events)
    assert "hotels" not in events and "finished_at" in runs[-1] and runs[-1]["usage_usd"] == 0.0


def test_stage1_zero_items_goes_to_stage2_without_stamping(db):
    events, runs = db
    c, n = run(None, [0.0, 0.0, 0.009], events=events, per_run=[[], [{"search_query": "Amankila", "name": "Amankila", "star_rating": 5}]])
    assert len(c.inputs) == 2 and n == 1 and events.count("hotels") == 1
    assert events.index("hotels") > events.index("spend", events.index("spend") + 1)  # stamped only after stage 2
    assert runs[1]["usage_usd"] == 0.0 and runs[1]["item_count"] == 0 and ROWS[-1]["google_query"] == "Amankila"


def test_zero_item_run_uses_short_usage_wait(db):
    events, _ = db
    c, _ = run(None, [0.0], events=events, per_run=[[], [], []])
    assert c.gets == 3 * 2  # zero-item wait 1s / poll 1s => 2 gets per run, not the 4 of the full wait


def test_delete_after_hotels_upsert_and_commit(db):
    events, _ = db
    run([ITEM], [0.009], events=events)
    assert events[-3:] == ["hotels", "commit", "delete"]


def test_all_items_unmapped_aborts(db):
    events, _ = db
    with pytest.raises(RuntimeError, match="maps to any query"):
        run([{"search_query": "zzz", "name": "x"}], [0.003], events=events)
    assert "hotels" not in events


def test_form_query():
    g = CFG["google"]
    assert google.form_query("plain", "Alila Seminyak", "bali", g) == "Alila Seminyak Bali"
    assert google.form_query("name_only", "Alila Seminyak", "bali", g) == "Alila Seminyak"
    assert google.form_query("prefixed", "Alila Seminyak", "bali", g) == "hotels in Alila Seminyak Bali"


def test_all_hit_at_stage1_one_run(db):
    events, runs = db
    c, _ = run([ITEM], [0.009], events=events)
    assert len(c.inputs) == 1 and runs[0]["input"]["query_form"] == "plain" and len(runs) == 2


def test_rescued_at_stage2(db):
    events, runs = db
    c, _ = run(None, [0.009], events=events, per_run=[
        [{"search_query": "Amankila Bali", "name": "Totally Other Place"}],
        [{"search_query": "Amankila", "name": "Amankila", "star_rating": 5}]])
    assert len(c.inputs) == 2 and c.inputs[1]["queries"] == ["Amankila"]
    assert [r["input"]["query_form"] for r in runs if "input" in r] == ["plain", "name_only"]
    assert ROWS[-1]["google_query"] == "Amankila" and ROWS[-1]["google_star_rating"] == 5


def test_rescued_only_at_stage3_and_zero_item_stage2_is_not_abort(db):
    events, runs = db
    c, _ = run(None, [0.009], events=events, per_run=[
        [{"search_query": "Amankila Bali", "name": "Zzz Qqq"}], [],
        [{"search_query": "hotels in Amankila Bali", "name": "Amankila", "star_rating": 5}]])
    assert len(c.inputs) == 3 and ROWS[-1]["google_query"] == "hotels in Amankila Bali"
    assert events.count("spend") == 3  # guard before every Apify run


def test_never_found_stamped_with_last_query_and_best_kept(db):
    events, _ = db
    c, _ = run(None, [0.009], events=events, per_run=[[{"search_query": "Amankila Bali", "name": "Zzz"}], [], []])
    r = ROWS[-1]
    assert len(c.inputs) == 3 and r["google_name"] is None and r["google_match_score"] < 75
    assert r["google_scraped_at"] is not None and "hotels" in events


def test_uncertain_score_not_retried(db):
    events, _ = db
    c, _ = run([{"search_query": "Amankila Bali", "name": "Amankilo"}], [0.009], events=events)
    assert len(c.inputs) == 1 and 75 <= ROWS[-1]["google_match_score"] < 90


def test_retry_keeps_best_across_stages(db):
    events, _ = db
    run(None, [0.009], events=events, per_run=[
        [{"search_query": "Amankila Bali", "name": "Amanda"}],
        [{"search_query": "Amankila", "name": "Zzz"}], [{"search_query": "hotels in Amankila Bali", "name": "Zzz"}]])
    r = ROWS[-1]
    assert r["google_query"] == "Amankila Bali" and r["google_match_score"] > 0


def test_spend_guard_null_usage_and_cap():
    class C:
        def __init__(self, missing, spent):
            self.missing, self.spent = missing, spent

        def execute(self, q):
            self.q = q
            return self

        def fetchone(self):
            return (self.missing,) if "run_id" in self.q and self.missing else None if "run_id" in self.q else (self.spent,)

    google.check_spend(C(None, 1), CFG)
    with pytest.raises(RuntimeError, match="where run_id = 'abc'"):
        google.check_spend(C("abc", 1), CFG)
    with pytest.raises(RuntimeError, match="max_spend"):
        google.check_spend(C(None, 85), CFG)


TWO = [(1, "Amankila", "bali"), (2, "Zeta Place", "bali")]
HIT1 = {"search_query": "Amankila Bali", "name": "Amankila", "star_rating": 5}
MISS2 = {"search_query": "Zeta Place Bali", "name": "Qqq"}


def test_stage2_failure_keeps_stage1_hits_stamped_misses_not(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="FAILED"):
        c = FakeClient(None, [0.009], events=events)
        c.per_run, c.statuses = [[HIT1, MISS2], []], ["SUCCEEDED", "FAILED"]
        google.process_chunk(c, Conn(events), TWO, CFG)
    assert [r["hotel_id"] for r in ROWS] == [1]
    assert events.count("hotels") == 1


def test_stage3_failure_keeps_earlier_stamps_no_double_stamp(db):
    events, _ = db
    other = [{"search_query": "Zeta Place", "name": "Zeta Place", "star_rating": 3}]
    c = FakeClient(None, [0.009], events=events)
    c.per_run, c.statuses = [[HIT1, MISS2], other, []], ["SUCCEEDED", "SUCCEEDED", "FAILED"]
    google.process_chunk(c, Conn(events), TWO, CFG)  # all resolved by stage 2: stage 3 skipped
    assert [r["hotel_id"] for r in ROWS] == [1, 2] and len(c.inputs) == 2
    ROWS.clear()
    c = FakeClient(None, [0.009], events=events)
    c.per_run, c.statuses = [[HIT1, MISS2], [], []], ["SUCCEEDED", "SUCCEEDED", "FAILED"]
    with pytest.raises(RuntimeError, match="FAILED"):
        google.process_chunk(c, Conn(events), TWO, CFG)
    assert [r["hotel_id"] for r in ROWS] == [1]


def test_per_stage_order_upsert_commit_delete(db):
    events, _ = db
    c = FakeClient(None, [0.009], events=events)
    c.per_run = [[HIT1, MISS2], [], []]
    google.process_chunk(c, Conn(events), TWO, CFG)
    i = events.index("hotels")
    assert events[i:i + 3] == ["hotels", "commit", "delete"]
    assert events.count("hotels") == 2  # stage 1 hit, then leftover miss at the end


def test_query_forms_recorded_and_unknown_form_rejected(db):
    events, runs = db
    run([ITEM], [0.009], events=events)
    assert runs[0]["input"]["query_forms"] == ["plain", "name_only", "prefixed"]
    bad = CFG | {"google": CFG["google"] | {"query_forms": ["plain", "bogus"]}}
    with pytest.raises(ValueError, match="bogus"):
        run([ITEM], [0.009], cfg=bad, events=events)


def test_skipped_stage_dedup_then_stage3(db):
    events, runs = db
    hotels = [(1, "Amankila Bali", "bali")]  # plain == name_only
    c, _ = run(None, [0.009], hotels=hotels, events=events, per_run=[
        [{"search_query": "Amankila Bali", "name": "Zzz"}],
        [{"search_query": "hotels in Amankila Bali", "name": "Amankila Bali", "star_rating": 4}]])
    assert [r["input"]["query_form"] for r in runs if "input" in r] == ["plain", "prefixed"]
    assert ROWS[-1]["google_query"] == "hotels in Amankila Bali"


def test_all_items_unmapped_at_retry_is_miss_with_warning(db, caplog):
    events, _ = db
    run(None, [0.009], events=events, per_run=[[{"search_query": "Amankila Bali", "name": "Zzz"}],
                                               [{"search_query": "nope", "name": "Amankila"}], []])
    assert "did not map" in caplog.text and ROWS[-1]["google_name"] is None
