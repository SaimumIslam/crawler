"""Engine runs multiple workers and dedupe stays exact under concurrency.

A fake fetcher serves an index page linking to N pages that each contain the SAME
shared item plus one unique item. With many workers racing to store the shared
item, the atomic dedupe must keep exactly one copy.
"""
from crawler.config import SiteTemplate
from crawler.crawl.engine import CrawlEngine
from crawler.fetch.base import FetchResult
from crawler.store.db import Store

PAGES = 20


def _html(url: str) -> str:
    if url.endswith("/index"):
        links = "".join(
            f'<a class="item-page" href="http://fake/p{i}"></a>' for i in range(PAGES)
        )
        return f"<html><body>{links}</body></html>"
    i = url.rsplit("/p", 1)[1]
    return (
        '<html><body>'
        '<div class="item"><span class="t">SHARED</span></div>'
        f'<div class="item"><span class="t">item-{i}</span></div>'
        '</body></html>'
    )


class _FakeFetcher:
    def fetch(self, url: str) -> FetchResult:
        return FetchResult(url=url, status_code=200, text=_html(url),
                           tier="http", ok=True, final_url=url)

    def close(self) -> None:
        pass


def _template(concurrency: int, site: str) -> SiteTemplate:
    return SiteTemplate.model_validate({
        "name": site,  # dedupe is scoped by site; keep tests independent
        "start_urls": ["http://fake/index"],
        "allow_domains": ["fake"],
        "follow": {"link_selector": "a.item-page", "max_depth": 1, "max_pages": 100},
        "extract": {
            "mode": "selectors",
            "item_selector": "div.item",
            "fields": {"title": {"css": "span.t::text"}},
        },
        "fetch": {"concurrency": concurrency, "respect_robots": False,
                  "rate_limit_per_sec": 1000},
    })


def _run(concurrency: int, site: str):
    store = Store()  # temp db via conftest env
    engine = CrawlEngine(_template(concurrency, site), store, ignore_robots=True)
    engine._make_fetcher = lambda: _FakeFetcher()  # inject the fake
    run_id = store.start_run(site)
    stats = engine.run(run_id)
    recs = store.records_for_run(run_id)
    engine.close()
    store.close()
    return stats, recs


def test_concurrent_crawl_dedupes_exactly():
    stats, recs = _run(concurrency=8, site="fake-c8")
    titles = [r["title"] for r in recs]
    # index + PAGES fetched
    assert stats["pages"] == PAGES + 1
    # PAGES unique items + exactly one SHARED, none duplicated
    assert stats["records_new"] == PAGES + 1
    assert len(recs) == PAGES + 1
    assert titles.count("SHARED") == 1


def test_result_matches_single_worker():
    """Concurrency must not change what gets stored vs. a single worker."""
    stats1, recs1 = _run(concurrency=1, site="fake-c1")
    assert stats1["records_new"] == PAGES + 1
    assert sorted(r["title"] for r in recs1).count("SHARED") == 1
