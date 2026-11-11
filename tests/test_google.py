from hotel_study.google import group_results


def test_group_results_by_search_query():
    items = [{"search_query": "a", "name": "1"}, {"search_query": "b", "name": "2"}, {"search_query": "a", "name": "3"}]
    g = group_results(items)
    assert [i["name"] for i in g["a"]] == ["1", "3"] and len(g["b"]) == 1


import pytest

from hotel_study import google

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
    def commit(self):
        pass


@pytest.fixture
def db(monkeypatch):
    events, runs = [], []
    monkeypatch.setattr(google, "upsert", lambda conn, t, rows, k: runs.append(rows[0]))
    monkeypatch.setattr(google, "upsert_rows", lambda conn, rows: events.append(("hotels", rows)))
    monkeypatch.setattr(google.time, "sleep", lambda s: None)
    return events, runs


CHUNK = [(1, "Amankila", "Amankila Bali")]
ITEM = {"search_query": "Amankila Bali", "name": "Amankila", "star_rating": 5}


def test_usage_poll_succeeds_after_retries(db):
    events, runs = db
    c = FakeClient([ITEM], [0.0, 0.0, 0.009], events=events)
    assert google.process_chunk(c, Conn(), CHUNK, CFG) == 1
    assert runs[-1]["usage_usd"] == 0.009


def test_usage_missing_raises_and_never_writes_zero(db):
    events, runs = db
    c = FakeClient([ITEM], [0.0], events=events)
    with pytest.raises(RuntimeError, match="usage"):
        google.process_chunk(c, Conn(), CHUNK, CFG)
    assert "usage_usd" not in runs[-1] and not events


def test_failed_run_leaves_hotels_unstamped(db):
    events, runs = db
    with pytest.raises(RuntimeError, match="FAILED"):
        google.process_chunk(FakeClient([], [0.0], status="FAILED", events=events), Conn(), CHUNK, CFG)
    assert not events and "finished_at" in runs[-1]


def test_delete_after_hotels_upsert(db):
    events, _ = db
    google.process_chunk(FakeClient([ITEM], [0.009], events=events), Conn(), CHUNK, CFG)
    assert [e if isinstance(e, str) else e[0] for e in events] == ["hotels", "delete"]


def test_all_items_unmapped_aborts(db):
    events, _ = db
    with pytest.raises(RuntimeError, match="maps to any query"):
        google.process_chunk(FakeClient([{"search_query": "zzz", "name": "x"}], [0.003], events=events), Conn(), CHUNK, CFG)
    assert events == []


def test_query_prefix_recorded_and_applied(db, monkeypatch):
    from hotel_study.matching import build_query
    assert "hotels in " + build_query("Alila Seminyak", "bali") == "hotels in Alila Seminyak Bali"
    events, runs = db
    google.process_chunk(FakeClient([ITEM], [0.009], events=events), Conn(), CHUNK,
                         CFG | {"google": CFG["google"] | {"query_prefix": "hotels in "}})
    assert runs[0]["input"]["query_prefix"] == "hotels in "


def test_spend_guard_null_usage_and_cap():
    class C:
        def __init__(self, a, b): self.v = [a, b]
        def execute(self, q):
            self.q = q
            return self
        def fetchone(self):
            return (self.v[0] if "count" in self.q else self.v[1],)
    google.check_spend(C(0, 1), CFG)
    with pytest.raises(RuntimeError, match="NULL"):
        google.check_spend(C(1, 1), CFG)
    with pytest.raises(RuntimeError, match="max_spend"):
        google.check_spend(C(0, 85), CFG)
