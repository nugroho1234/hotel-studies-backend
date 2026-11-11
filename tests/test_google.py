from hotel_study.google import group_results


def test_group_results_by_search_query():
    items = [{"search_query": "a", "name": "1"}, {"search_query": "b", "name": "2"}, {"search_query": "a", "name": "3"}]
    g = group_results(items)
    assert [i["name"] for i in g["a"]] == ["1", "3"] and len(g["b"]) == 1
