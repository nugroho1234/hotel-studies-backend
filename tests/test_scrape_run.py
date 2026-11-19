import pytest

from hotel_study import reviews
from hotel_study.reviews import build_run_input, final_sample, hotel_status, make_batches, strip_fragment

CFG = {"review_window": {"start": "2023-01-01", "end": "2025-12-31"}, "languages": ["en", "id"], "batch_size": 2,
       "eligibility": {"threshold": 100}, "reviews_per_hotel": 300, "max_spend_usd": 85, "delete_remote_datasets": True,
       "google": {"usage_poll_seconds": 0, "usage_max_wait_seconds": 0, "usage_zero_item_wait_seconds": 0}}


@pytest.mark.parametrize("args,want", [
    ((300, 5, 5), ("complete", True)), ((80, 3, 5), ("complete", False)), ((80, 5, 5), ("needs_pass2", None)),
    ((120, 5, 5), ("needs_pass2", True)), ((120, 3, 5), ("complete", True)),
    ((250, 99, None), ("complete", True)), ((80, 99, None), ("complete", False)),
])
def test_hotel_status(args, want):
    assert hotel_status(*args) == want


def test_final_sample():
    assert final_sample("complete", True) and not final_sample("complete", False)
    assert not final_sample("needs_pass2", True) and not final_sample("needs_pass2", None)


def test_strip_fragment():
    assert strip_fragment("https://x/Hotel_Review-g1-d2-Reviews-A.html#/chat/abc") == "https://x/Hotel_Review-g1-d2-Reviews-A.html"
    assert strip_fragment("https://x/a.html") == "https://x/a.html"


def test_build_run_input():
    i = build_run_input(["u1", "u2"], CFG, None)
    assert i["since"] == "2023-01-01" and i["startUrls"] == ["u1", "u2"]
    assert not {"languages", "maxItems", "ratings", "endPage"} & set(i)
    assert build_run_input(["u1"], CFG, 7)["endPage"] == 7
    with pytest.raises(ValueError):
        build_run_input(["a", "b", "c"], CFG, None)


def test_make_batches_one_cell_each():
    hs = [(1, "S", 3, "u1"), (2, "S", 3, "u2"), (3, "S", 3, "u3"), (4, "S", 4, "u4")]
    assert [[h[0] for h in b] for b in make_batches(hs, 2)] == [[1, 2], [3], [4]]


# ---- run loop with fakes ----
class R:
    def __init__(self, status="SUCCEEDED", usage=0.5):
        self.id, self.default_dataset_id, self.status, self.usage_total_usd = "run1", "ds1", status, usage


class Client:
    def __init__(self, items, statuses=("SUCCEEDED",), events=None):
        self.items, self.statuses, self.events = items, list(statuses), events if events is not None else []
        self.starts = 0

    def actor(self, _): return self
    def run(self, _): return self
    def dataset(self, _): return self

    def start(self, run_input):
        self.starts += 1
        self.run_input = run_input
        return R()

    def wait_for_finish(self): return R(self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])
    def get(self): return R()
    def iterate_items(self): return iter(self.items)
    def delete(self): self.events.append("delete")


class Conn:
    def __init__(self, events, hotels):
        self.events, self.hotels = events, hotels

    def execute(self, sql, args=()):
        if sql.lstrip().startswith("UPDATE hotels"):
            self.events.append(("hotel", args))
        return self

    def fetchall(self): return self.hotels
    def commit(self): pass


def item(rid, hid, page=1):
    return {"id": rid, "location": {"locationId": hid}, "inputSource": {"pageNumber": page}}


@pytest.fixture
def env(monkeypatch):
    ev = []
    monkeypatch.setattr(reviews, "upsert", lambda c, t, rows, k: ev.append((t, rows[0].get("pass"), "finished_at" in rows[0])))
    monkeypatch.setattr(reviews, "check_spend", lambda c, cfg: ev.append("spend"))
    monkeypatch.setattr(reviews, "ingest_items", lambda c, rid, items, cfg: ev.append("ingest") or
                        {1: dict(n_raw=150, n_out_of_window=10, n_other_language=5, n_eligible=135, n_bad_date=0)})
    return ev


def test_batch_order_and_hotel_update(env):
    client = Client([item(1, 1, 5), item(2, 1, 7)], events=env)
    ok = reviews.run_batch(client, Conn(env, []), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert ok
    kinds = [e if isinstance(e, str) else e[0] for e in env]
    # scrape_runs row (start) before ingest; hotels updated after ingest; finish row; dataset deleted last
    assert kinds == ["spend", "scrape_runs", "ingest", "hotel", "scrape_runs", "delete"]
    assert env[1][2] is False and env[4][2] is True and env[1][1] == "eligibility_test"
    assert client.run_input["startUrls"] == ["u1"] and "endPage" not in client.run_input
    args = env[3][1]
    assert 135 in args and True in args  # n_eligible and is_eligible/in_final_sample set


def test_retry_once_then_skip_no_reviews(env):
    client = Client([], statuses=("FAILED",), events=env)
    ok = reviews.run_batch(client, Conn(env, []), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert not ok and client.starts == 2
    assert "ingest" not in env and not any(isinstance(e, tuple) and e[0] == "hotel" for e in env)


def test_retry_succeeds(env):
    client = Client([item(1, 1)], statuses=("FAILED", "SUCCEEDED"), events=env)
    assert reviews.run_batch(client, Conn(env, []), [(1, "S", 3, "u1")], CFG, "eligibility_test", None) and client.starts == 2


def test_spend_guard_stops(env, monkeypatch):
    def boom(c, cfg): raise RuntimeError("max_spend_usd 85 reached")
    monkeypatch.setattr(reviews, "check_spend", boom)
    client = Client([], events=env)
    with pytest.raises(RuntimeError, match="max_spend"):
        reviews.run_batch(client, Conn(env, []), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert client.starts == 0


def test_resume_skips_scraped(env, monkeypatch):
    # the selection query only returns not_scraped hotels; an empty result means no runs
    client = Client([], events=env)
    monkeypatch.setattr(reviews, "ApifyClient", lambda t: client)
    monkeypatch.setattr(reviews, "get_secret", lambda n: "x")
    monkeypatch.setattr(reviews, "load_config", lambda: CFG | {"end_page": None})
    out = reviews.scrape_reviews("1", True, None, None, conn=Conn(env, []))
    assert client.starts == 0 and "0 hotels" in out
