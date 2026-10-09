"""Crawl engine: frontier + fetch (tiered/stealth) + extract + store + dedupe.

Flow per run:
  1. Seed the frontier with start_urls at depth 0.
  2. Up to `fetch.concurrency` worker threads pull URLs, respect robots + rate
     limit, and fetch via a per-worker tiered fetcher.
  3. Extract records; insert new (unseen-hash) ones into the store (dedupe is
     atomic, so concurrent workers never double-store an item).
  4. Enqueue followed links (link_selector) up to max_depth, and the paginate
     link (same depth). Stop at max_pages or an idle, empty frontier.

Concurrency is bounded by `fetch.concurrency`; each worker owns its own fetcher
(hence its own browser instances if it escalates). `manual` CAPTCHA mode forces a
single worker so at most one human-facing browser window opens at a time.
"""
from __future__ import annotations

import re
import threading
from collections import deque
from urllib.parse import urljoin, urlparse

from parsel import Selector

from ..config import SiteTemplate
from ..fetch.escalation import TieredFetcher
from ..fetch.proxies import ProxyPool
from ..logging_conf import clear_current_run, get_logger, set_current_run
from ..store.db import Store
from .robots import RateLimiter, RobotsCache

log = get_logger("engine")


class _AbortRun(Exception):
    """Raised to abort the whole run (e.g. captcha.on_detect == 'stop')."""


class CrawlEngine:
    def __init__(
        self,
        template: SiteTemplate,
        store: Store,
        ignore_robots: bool = False,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.t = template
        self.store = store
        # Set by the dashboard's Stop button; workers finish their page then exit.
        self.stop_event = stop_event or threading.Event()
        self.ignore_robots = ignore_robots or not template.fetch.respect_robots
        self.proxies = ProxyPool()
        self.limiter = RateLimiter(template.fetch.rate_limit_per_sec)
        self.robots = RobotsCache(self._robots_fetch_text)
        self._deny = [re.compile(p) for p in template.deny_patterns]
        self._fetchers: list[TieredFetcher] = []
        self._fetchers_lock = threading.Lock()

    def _make_fetcher(self) -> TieredFetcher:
        f = TieredFetcher(
            self.t.fetch, site=self.t.name, proxies=self.proxies,
            captcha=self.t.captcha,
        )
        with self._fetchers_lock:
            self._fetchers.append(f)
        return f

    # robots.txt is fetched through Tier 1 AND the proxy pool, so it shares the
    # same egress IP as the crawl (no origin-IP leak when proxying).
    def _robots_fetch_text(self, url: str) -> str | None:
        from ..fetch.http_curl import CurlFetcher

        proxy = self.proxies.next_proxy(session_key=f"{self.t.name}:robots")
        r = CurlFetcher(self.t.fetch.timeout_sec, self.t.fetch.impersonate).fetch(
            url, proxy=proxy
        )
        return r.text if r.ok else None

    # ----- URL policy ------------------------------------------------------- #

    def _allowed_domain(self, url: str) -> bool:
        host = urlparse(url).netloc
        if not self.t.allow_domains:
            return True
        return any(host == d or host.endswith("." + d) for d in self.t.allow_domains)

    def _denied(self, url: str) -> bool:
        return any(p.search(url) for p in self._deny)

    def _should_visit(self, url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        if not self._allowed_domain(url) or self._denied(url):
            return False
        if not self.ignore_robots and not self.robots.allowed(url):
            log.debug("robots.txt disallows %s", url)
            return False
        return True

    # ----- link discovery --------------------------------------------------- #

    def _find_links(self, html: str, base_url: str) -> tuple[list[str], str | None]:
        sel = Selector(text=html)
        links: list[str] = []
        f = self.t.follow
        if f.link_selector:
            for href in sel.css(f.link_selector).xpath("@href").getall():
                links.append(urljoin(base_url, href))
        elif f.link_xpath:
            for href in sel.xpath(f.link_xpath).xpath("@href").getall():
                links.append(urljoin(base_url, href))
        next_url = None
        if f.paginate:
            nxt = sel.css(f.paginate).xpath("@href").get()
            if nxt:
                next_url = urljoin(base_url, nxt)
        return links, next_url

    # ----- run -------------------------------------------------------------- #

    def run(self, run_id: int) -> dict:
        from ..extract.selectors import extract_records

        use_llm = self.t.extract.mode == "llm"
        llm = None
        if use_llm:
            from ..extract.llm import LLMExtractor

            llm = LLMExtractor()

        max_pages = self.t.follow.max_pages
        max_depth = self.t.follow.max_depth
        # manual CAPTCHA solving is human-facing; keep it to one window at a time.
        n_workers = 1 if self.t.captcha.on_detect == "manual" else max(1, self.t.fetch.concurrency)

        if self.ignore_robots:
            log.warning("robots.txt is being IGNORED for site '%s' (opt-in override)", self.t.name)

        # Shared state guarded by `cv`. Fetch/extract happen OUTSIDE the lock.
        cv = threading.Condition()
        frontier: deque[tuple[str, int]] = deque((u, 0) for u in self.t.start_urls)
        seen: set[str] = set(self.t.start_urls)
        state = {"started": 0, "done": 0, "new": 0, "in_flight": 0, "stopped": False}
        abort: list[_AbortRun] = []

        def next_task() -> tuple[str, int] | None:
            with cv:
                while True:
                    if state["stopped"] or self.stop_event.is_set():
                        return None
                    if frontier and state["started"] < max_pages:
                        state["started"] += 1
                        state["in_flight"] += 1
                        return frontier.popleft()
                    # nothing to hand out right now
                    if state["in_flight"] == 0 or state["started"] >= max_pages:
                        cv.notify_all()  # release peers waiting below
                        return None
                    cv.wait()  # another worker may still add links

        def enqueue(items: list[tuple[str, int]]) -> None:
            with cv:
                for url, depth in items:
                    if url not in seen:
                        seen.add(url)
                        frontier.append((url, depth))
                cv.notify_all()

        def handle(url: str, depth: int, fetcher: TieredFetcher) -> None:
            if not self._should_visit(url):
                return
            self.limiter.wait(
                url,
                extra_delay=None if self.ignore_robots else self.robots.crawl_delay(url),
            )
            result = fetcher.fetch(url)
            with cv:
                state["done"] += 1
            self.store.record_page(
                run_id, url, result.status_code, result.tier, result.blocked, result.error,
            )
            self.store.bump_run_counts(run_id, pages=1)  # live progress for the dashboard

            if result.captcha and not result.ok:
                mode = self.t.captcha.on_detect
                if mode == "stop":
                    log.error("CAPTCHA (%s) at %s and captcha.on_detect=stop; aborting run",
                              result.captcha_vendor, url)
                    raise _AbortRun(f"CAPTCHA ({result.captcha_vendor}) at {url}")
                log.warning("CAPTCHA (%s) at %s not cleared (mode=%s); skipping URL",
                            result.captcha_vendor, url, mode)
                return
            if not result.ok:
                log.warning("Fetch failed [%s] %s (status=%s blocked=%s)",
                            result.tier, url, result.status_code, result.blocked)
                return

            base = result.final_url or url
            records = (llm.extract if use_llm else extract_records)(
                result.text, self.t.extract, base_url=base
            )
            new_here = 0
            for rec in records:
                if self.store.add_record(run_id, self.t.name, base, rec):
                    new_here += 1
            with cv:
                state["new"] += new_here
                total_new = state["new"]
            if new_here:
                self.store.bump_run_counts(run_id, records_new=new_here)
            log.info("[%s] %s -> %d records (%d new) via %s",
                     depth, url, len(records), total_new, result.tier)

            links, next_url = self._find_links(result.text, base)
            candidates: list[tuple[str, int]] = []
            if depth < max_depth:
                candidates.extend((link, depth + 1) for link in links)
            if next_url:
                candidates.append((next_url, depth))  # pagination keeps same depth
            # Filter here (robots is cached, so this is cheap) so the frontier holds
            # only visitable URLs and the max_pages budget tracks real fetches.
            to_add = [(u, d) for (u, d) in candidates if self._should_visit(u)]
            if to_add:
                enqueue(to_add)

        def worker() -> None:
            set_current_run(run_id)  # tag this worker's logs with the run (live log view)
            fetcher = self._make_fetcher()
            try:
                while True:
                    item = next_task()
                    if item is None:
                        return
                    try:
                        handle(item[0], item[1], fetcher)
                    except _AbortRun as e:
                        with cv:
                            if not abort:
                                abort.append(e)
                            state["stopped"] = True
                            cv.notify_all()
                    except Exception:  # noqa: BLE001
                        log.exception("worker error on %s", item[0])
                    finally:
                        with cv:
                            state["in_flight"] -= 1
                            cv.notify_all()
            finally:
                fetcher.close()
                clear_current_run()

        threads = [threading.Thread(target=worker, name=f"crawl-{i}") for i in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if abort:
            raise abort[0]
        return {"pages": state["done"], "records_new": state["new"]}

    def close(self) -> None:
        # Workers close their own fetchers; this handles any created but unused.
        with self._fetchers_lock:
            fetchers = list(self._fetchers)
            self._fetchers.clear()
        for f in fetchers:
            try:
                f.close()
            except Exception:
                pass
