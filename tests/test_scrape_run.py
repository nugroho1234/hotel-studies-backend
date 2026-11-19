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
import itertools

from hotel_study.google import SpendLimitReached

_ids = itertools.count(1)


class R:
    def __init__(self, rid="run1", status="SUCCEEDED", usage=0.5):
        self.id, self.default_dataset_id, self.status, self.usage_total_usd = rid, "ds-" + rid, status, usage


class Client:
    def __init__(self, items, statuses=("SUCCEEDED",), events=None, usage=0.5, wait_none=False):
        self.items, self.statuses, self.events, self.usage, self.wait_none = items, list(statuses), events if events is not None else [], usage, wait_none
        self.starts, self.cur, self.deleted, self.get_status, self.get_fail = 0, None, [], "SUCCEEDED", False

    def actor(self, _): return self
    def run(self, rid): self.cur = rid; return self
    def dataset(self, did): self.ds = did; return self

    def start(self, run_input):
        self.starts += 1
        self.run_input = run_input
        self.cur = f"run{next(_ids)}"
        return R(self.cur)

    def wait_for_finish(self):
        if self.wait_none:
            return None
        return R(self.cur, self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0], self.usage)

    def get(self):
        if self.get_fail:
            raise RuntimeError("api down")
        return R(self.cur, self.get_status, self.usage)

    def iterate_items(self): self.events.append("fetch"); return iter(self.items)
    def delete(self): self.events.append("delete"); self.deleted.append(self.ds)


class Conn:
    def __init__(self, events, hotels=(), unfinished=(), todo=()):
        self.events, self.hotels, self.unfinished, self.todo, self.last = events, list(hotels), list(unfinished), list(todo), ""

    def execute(self, sql, args=()):
        self.last = sql.lstrip()
        if self.last.startswith("UPDATE hotels"):
            self.events.append(("hotel", args))
        return self

    def fetchall(self):
        if "finished_at IS NULL" in self.last:
            return self.unfinished
        if "ANY" in self.last:
            return [(h,) for h in self.todo]
        return self.hotels  # the SELECT already filters scrape_status; the fake returns what a DB would

    def commit(self): pass


def item(rid, hid, page=1):
    return {"id": rid, "location": {"locationId": hid}, "inputSource": {"pageNumber": page}}


def counts(n_raw=150, n_elig=135):
    return {1: dict(n_raw=n_raw, n_out_of_window=10, n_other_language=5, n_eligible=n_elig, n_bad_date=0)}


class Ev(list):
    pass


@pytest.fixture
def env(monkeypatch):
    ev, rows = Ev(), []
    monkeypatch.setattr(reviews, "upsert", lambda c, t, r, k: (ev.append((t, r[0].get("pass"), "finished_at" in r[0])), rows.append(r[0])))
    monkeypatch.setattr(reviews, "check_spend", lambda c, cfg: ev.append("spend"))
    ev.rows, ev.ret = rows, [counts()]
    monkeypatch.setattr(reviews, "ingest_items", lambda c, rid, items, cfg: ev.append("ingest") or ev.ret[0])
    return ev


def hotel_events(env):
    return [e[1] for e in env if isinstance(e, tuple) and e[0] == "hotel"]


def test_batch_order_and_hotel_update(env):
    client = Client([item(1, 1, 5), item(2, 1, 7)], events=env)
    ok = reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert ok == 1
    kinds = [e if isinstance(e, str) else e[0] for e in env]
    # scrape_runs row (start) before ingest; hotels updated after ingest; finish row; dataset deleted last
    assert kinds == ["spend", "scrape_runs", "fetch", "ingest", "hotel", "scrape_runs", "delete"]
    assert env[1][2] is False and env[5][2] is True and env[1][1] == "eligibility_test"
    assert client.run_input["startUrls"] == ["u1"] and "endPage" not in client.run_input
    n_raw, n_oow, n_other, n_elig, is_eligible, status, in_final_sample, hotel_id = hotel_events(env)[0]
    assert (n_raw, n_oow, n_other, n_elig, hotel_id) == (150, 10, 5, 135, 1)
    assert is_eligible is True and status == "complete" and in_final_sample is True


def test_not_eligible_not_in_final_sample(env):
    env.ret[0] = counts(60, 50)
    reviews.run_batch(Client([item(1, 1)], events=env), Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    _, _, _, _, is_eligible, status, in_final_sample, _ = hotel_events(env)[0]
    assert (is_eligible, status, in_final_sample) == (False, "complete", False)


def test_usage_written_and_null_when_unconfirmed(env):
    reviews.run_batch(Client([item(1, 1)], events=env, usage=0.5), Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert env.rows[-1]["usage_usd"] == 0.5 and env.rows[-1]["item_count"] == 1
    env.rows.clear()
    reviews.run_batch(Client([item(1, 1)], events=env, usage=0.0), Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert "usage_usd" not in env.rows[-1]  # NULL: check_spend will block further runs


def test_retry_once_then_skip_no_reviews(env):
    client = Client([], statuses=("FAILED",), events=env, usage=0.0)
    ok = reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert ok is None and client.starts == 2
    assert "ingest" not in env and not hotel_events(env)


def test_failed_run_finish_row_and_dataset_deleted(env):
    client = Client([], statuses=("FAILED",), events=env, usage=0.0)
    reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert len(client.deleted) == 2 and all(d.startswith("ds-run") for d in client.deleted)
    finish = [r for r in env.rows if "finished_at" in r]
    assert len(finish) == 2 and len({r["run_id"] for r in finish}) == 2 and all(r["item_count"] == 0 for r in finish)


def test_retry_succeeds(env):
    client = Client([item(1, 1)], statuses=("FAILED", "SUCCEEDED"), events=env, usage=0.5)
    assert reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None) == 1 and client.starts == 2


def test_apify_error_retried(env):
    from apify_client.errors import ApifyApiError

    class Flaky(Client):
        def start(self, run_input):
            if self.starts == 0:
                self.starts += 1
                raise OSError("network")
            return super().start(run_input)

    client = Flaky([item(1, 1)], events=env)
    assert reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None) == 1


def test_wait_none_is_failure_left_unfinished(env, monkeypatch):
    calls = []

    def guard(c, cfg):
        calls.append(1)
        if len(calls) > 1:
            raise SpendLimitReached("unfinished")
    monkeypatch.setattr(reviews, "check_spend", guard)
    client = Client([], events=env, wait_none=True)
    with pytest.raises(SpendLimitReached):  # the retry is refused until reconcile settles the unfinished row
        reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert client.starts == 1 and not [r for r in env.rows if "finished_at" in r]


def test_zero_items_hotel_stays_not_scraped(env):
    env.ret[0] = {}
    assert reviews.run_batch(Client([], events=env, usage=0.0), Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None) == 0
    assert not hotel_events(env)
    env.ret[0] = counts(0, 0)
    reviews.run_batch(Client([], events=env, usage=0.0), Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert not hotel_events(env)


def test_spend_guard_stops(env, monkeypatch):
    def boom(c, cfg): raise SpendLimitReached("max_spend_usd 85 reached")
    monkeypatch.setattr(reviews, "check_spend", boom)
    client = Client([], events=env)
    with pytest.raises(SpendLimitReached):
        reviews.run_batch(client, Conn(env), [(1, "S", 3, "u1")], CFG, "eligibility_test", None)
    assert client.starts == 0


def test_resume_only_not_scraped_hotels_are_selected(env, monkeypatch):
    client = Client([item(1, 1)], events=env)
    seen = []

    class C(Conn):
        def execute(self, sql, args=()):
            seen.append(sql)
            return super().execute(sql, args)

    monkeypatch.setattr(reviews, "ApifyClient", lambda t: client)
    monkeypatch.setattr(reviews, "get_secret", lambda n: "x")
    monkeypatch.setattr(reviews, "load_config", lambda: CFG | {"end_page": None})
    out = reviews.scrape_reviews("1", True, None, None, conn=C(env))
    assert client.starts == 0 and "0 hotels" in out
    assert any("scrape_status = 'not_scraped'" in q and "in_eligibility_test" in q for q in seen)


def _reconcile_env(env, monkeypatch, client, conn):
    monkeypatch.setattr(reviews, "ApifyClient", lambda t: client)
    monkeypatch.setattr(reviews, "get_secret", lambda n: "x")
    monkeypatch.setattr(reviews, "load_config", lambda: CFG | {"end_page": None})
    return reviews.scrape_reviews("1", True, None, None, conn=conn)


URL = "https://x/Hotel_Review-g1-d1-Reviews-A.html"


def test_reconcile_succeeded_unfinished_ingests_stored_dataset(env, monkeypatch):
    client = Client([item(1, 1)], events=env)
    conn = Conn(env, unfinished=[("runX", "ds-runX", {"startUrls": [URL]}, reviews.ACTOR)], todo=[1])
    _reconcile_env(env, monkeypatch, client, conn)
    assert client.starts == 0 and "ingest" in env and len(hotel_events(env)) == 1
    assert client.deleted == ["ds-runX"] and env.rows[-1]["run_id"] == "runX" and "finished_at" in env.rows[-1]


def test_reconcile_failed_unfinished_deletes_dataset_and_finishes(env, monkeypatch):
    client = Client([], events=env, usage=0.0)
    client.get_status = "FAILED"
    conn = Conn(env, unfinished=[("runX", "ds-runX", {"startUrls": [URL]}, reviews.ACTOR)])
    _reconcile_env(env, monkeypatch, client, conn)
    assert client.starts == 0 and "ingest" not in env and client.deleted == ["ds-runX"] and "finished_at" in env.rows[-1]


def test_reconcile_failure_aborts_without_new_run(env, monkeypatch):
    client = Client([], events=env)
    client.get_fail = True
    conn = Conn(env, hotels=[(1, "S", 3, URL)], unfinished=[("runX", "ds-runX", {}, reviews.ACTOR)])
    with pytest.raises(RuntimeError, match=r"update scrape_runs set finished_at = now\(\), usage_usd = .*where run_id = .runX."):
        _reconcile_env(env, monkeypatch, client, conn)
    assert client.starts == 0


def test_check_spend_refuses_unfinished_row():
    from hotel_study.google import check_spend

    class C:
        def execute(self, q): self.q = q; return self
        def fetchone(self): return ("abc",) if "finished_at IS NULL" in self.q else None

    with pytest.raises(SpendLimitReached, match="abc"):
        check_spend(C(), {"max_spend_usd": 85})


def test_reconcile_google_row_closed_without_ingest(env, monkeypatch):
    client = Client([item(1, 1)], events=env)
    conn = Conn(env, unfinished=[("g1", "ds-g1", {"queries": ["q"]}, "s-r/free-google-hotels-scraper")], todo=[1])
    _reconcile_env(env, monkeypatch, client, conn)
    assert client.starts == 0 and "ingest" not in env and "fetch" not in env
    assert client.deleted == ["ds-g1"] and env.rows[-1]["actor"] == "s-r/free-google-hotels-scraper" and "finished_at" in env.rows[-1]


def test_reconcile_google_row_unfetchable_aborts_with_hint(env, monkeypatch):
    client = Client([], events=env)
    client.get_fail = True
    conn = Conn(env, unfinished=[("g1", "ds-g1", {}, "s-r/free-google-hotels-scraper")])
    with pytest.raises(RuntimeError, match="where run_id = 'g1'"):
        _reconcile_env(env, monkeypatch, client, conn)
    assert client.starts == 0
