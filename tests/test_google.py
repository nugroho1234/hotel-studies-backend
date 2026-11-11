import pytest

from hotel_study import google
from hotel_study.google import group_results


def test_group_results_by_search_query():
    items = [{"search_query": "a", "name": "1"}, {"search_query": "b", "name": "2"}, {"search_query": "a", "name": "3"}]
    g = group_results(items)
    assert [i["name"] for i in g["a"]] == ["1", "3"] and len(g["b"]) == 1


CFG = {"google": {"top_results": 3, "hl": "en", "gl": "id", "fetch_details": False, "query_prefix": "",
                  "usage_poll_seconds": 1, "usage_max_wait_seconds": 3},
       "name_match": {"accept": 90, "uncertain": 75}, "delete_remote_datasets": True, "max_spend_usd": 85}


class R:
    def __init__(self, status="SUCCEEDED", usage=None):
        self.id, self.default_dataset_id, self.status, self.usage_total_usd = "r1", "d1", status, usage


class FakeClient:
    def __init__(self, items, usages, status="SUCCEEDED", events=None):
        self.items, self.usages, self.status, self.events = items, list(usages), status, events if events is not None else []

    def actor(self, _):
        return self

    def start(self, run_input):
        self.run_input = run_input
        return R()

    def run(self, _):
        return self

    def wait_for_finish(self):
        return R(self.status, 0.0)

    def get(self):
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


@pytest.fixture
def db(monkeypatch):
    events, runs = [], []
    monkeypatch.setattr(google, "upsert", lambda conn, t, rows, k: runs.append(rows[0]))
    monkeypatch.setattr(google, "upsert_rows", lambda conn, rows: events.append("hotels"))
    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    return events, runs


CHUNK = [(1, "Amankila", "Amankila Bali")]
ITEM = {"search_query": "Amankila Bali", "name": "Amankila", "star_rating": 5}


def test_usage_poll_succeeds_after_retries(db):
    events, runs = db
    c = FakeClient([ITEM], [0.0, 0.0, 0.009], events=events)
    assert google.process_chunk(c, Conn(events), CHUNK, CFG) == 1
    assert runs[-1]["usage_usd"] == 0.009


def test_items_but_usage_unconfirmed_raises_and_leaves_null(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="usage"):
        google.process_chunk(FakeClient([ITEM], [0.0], events=events), Conn(events), CHUNK, CFG)
    assert "usage_usd" not in runs[-1] and "hotels" not in events


def test_failed_run_unstamped_records_observed_usage(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="FAILED"):
        google.process_chunk(FakeClient([], [0.0], status="FAILED", events=events), Conn(events), CHUNK, CFG)
    assert "hotels" not in events and "finished_at" in runs[-1] and runs[-1]["usage_usd"] == 0.0


def test_zero_items_timeout_records_observed_and_aborts(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="no items"):
        google.process_chunk(FakeClient([], [0.0], events=events), Conn(events), CHUNK, CFG)
    assert "hotels" not in events and runs[-1]["usage_usd"] == 0.0 and runs[-1]["item_count"] == 0


def test_delete_after_hotels_upsert_and_commit(db):
    events, _ = db
    google.process_chunk(FakeClient([ITEM], [0.009], events=events), Conn(events), CHUNK, CFG)
    assert events[-3:] == ["hotels", "commit", "delete"]


def test_all_items_unmapped_aborts(db):
    events, _ = db
    with pytest.raises(RuntimeError, match="maps to any query"):
        google.process_chunk(FakeClient([{"search_query": "zzz", "name": "x"}], [0.003], events=events), Conn(events), CHUNK, CFG)
    assert "hotels" not in events


def test_chunk_queries_prefix_empty_and_set():
    hotels = [(1, "Alila Seminyak", "bali")]
    assert google.chunk_queries(hotels, CFG) == [(1, "Alila Seminyak", "Alila Seminyak Bali")]
    cfg = CFG | {"google": CFG["google"] | {"query_prefix": "hotels in "}}
    assert google.chunk_queries(hotels, cfg)[0][2] == "hotels in Alila Seminyak Bali"


def test_prefix_recorded_in_run_input(db):
    events, runs = db
    cfg = CFG | {"google": CFG["google"] | {"query_prefix": "hotels in "}}
    google.process_chunk(FakeClient([ITEM], [0.009], events=events), Conn(events), CHUNK, cfg)
    assert runs[0]["input"]["query_prefix"] == "hotels in "


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
