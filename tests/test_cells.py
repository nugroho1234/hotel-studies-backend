import pytest

from hotel_study.cells import assign_order, cell_done, next_batch_size, order_cells


def test_assign_order_deterministic_permutation_order_independent():
    ids = [50, 7, 31, 99, 2, 18]
    a = assign_order(ids, 42)
    assert a == assign_order(ids, 42)
    assert sorted(a.values()) == list(range(1, len(ids) + 1))
    assert set(a) == set(ids)
    assert a == assign_order(list(reversed(ids)), 42)


def test_cell_done():
    assert cell_done(30, 30) and cell_done(31, 30) and not cell_done(29, 30)


@pytest.mark.parametrize("args,want", [
    ((3, 0.5, 10, 20), 6), ((20, 0.5, 10, 20), 10), ((3, 0.5, 10, 2), 2),
    ((3, 0, 10, 20), 10), ((3, 0, 10, 4), 4), ((1, 1.0, 10, 5), 1),
])
def test_next_batch_size(args, want):
    assert next_batch_size(*args) == want


class FakeConn:
    def __init__(self, reviews, hotels):
        self.reviews, self.hotels, self.log, self.committed = reviews, hotels, [], False

    def execute(self, q, params=None):
        self.log.append((str(q), params))
        self._rows = [(1,)] if "FROM reviews" in str(q) and self.reviews else (
            self.hotels if "FROM hotels" in str(q) else [])
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def commit(self):
        self.committed = True


def test_order_cells_refuses_when_reviews_exist():
    c = FakeConn(True, [])
    with pytest.raises(RuntimeError, match="reviews"):
        order_cells(conn=c)
    assert not c.committed and not any("UPDATE" in q for q, _ in c.log)


def test_order_cells_assigns_per_cell_and_is_repeatable():
    hotels = [(i, "Bali", 4) for i in (5, 3, 9, 1)] + [(20, "Jakarta", 5)]
    runs = []
    for _ in range(2):
        c = FakeConn(False, hotels)
        order_cells(conn=c)
        runs.append([p for q, p in c.log if "SET cell_order = %s" in q])
        assert c.committed
    assert runs[0] == runs[1]
    bali = sorted(p[0] for p in runs[0] if p[2] in (5, 3, 9, 1))
    assert bali == [1, 2, 3, 4]
    assert [p for p in runs[0] if p[2] == 20] == [(1, True, 20)]
    assert sum(p[1] for p in runs[0] if p[2] in (5, 3, 9, 1)) == 3  # eligibility_test_size=3
    # NULL-out precedes assignment within the transaction
    qs = [q for q, _ in c.log]
    assert qs.index(next(q for q in qs if "cell_order = NULL" in q)) < qs.index(next(q for q in qs if "SET cell_order = %s" in q))


def test_order_cells_limit_and_destination():
    hotels = [(1, "Bali", 3), (2, "Bali", 4), (3, "Jakarta", 4)]
    c = FakeConn(False, hotels)
    order_cells(limit=1, conn=c)
    assert len([1 for q, _ in c.log if "SET cell_order = %s" in q]) == 1
    c = FakeConn(False, hotels)
    order_cells(destination="jakarta", conn=c)
    assert [p[2] for q, p in c.log if "SET cell_order = %s" in q] == [3]
