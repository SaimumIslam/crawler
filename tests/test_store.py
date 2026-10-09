from crawler.store.db import Store, content_hash


def test_dedupe_across_runs():
    store = Store()  # temp db from conftest env
    rec = {"title": "X", "price": "1.00"}

    run1 = store.start_run("site-a")
    assert store.add_record(run1, "site-a", "u1", rec) is True   # new
    assert store.add_record(run1, "site-a", "u1", rec) is False  # dup within run
    store.finish_run(run1)

    run2 = store.start_run("site-a")
    assert store.add_record(run2, "site-a", "u1", rec) is False  # dup across runs
    assert store.add_record(run2, "site-a", "u1", {"title": "Y"}) is True  # new item
    store.finish_run(run2)
    store.close()


def test_hash_is_order_independent():
    a = content_hash("s", {"a": 1, "b": 2})
    b = content_hash("s", {"b": 2, "a": 1})
    assert a == b


def test_hash_site_scoped():
    assert content_hash("s1", {"a": 1}) != content_hash("s2", {"a": 1})
