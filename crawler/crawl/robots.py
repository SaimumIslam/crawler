"""Politeness: robots.txt compliance and per-domain rate limiting.

- `RobotsCache` fetches and caches robots.txt per host and answers `allowed()`.
- `RateLimiter` enforces a minimum spacing between requests to the same domain.

robots.txt is respected by default. An explicit per-job override
(`respect_robots: false` / `--ignore-robots`) is honored but the caller is
expected to log it — this crawler is for authorized targets.
"""
from __future__ import annotations

import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

from ..logging_conf import get_logger

log = get_logger("robots")

_UA = "*"


class RobotsCache:
    def __init__(self, fetch_text) -> None:
        # fetch_text(url) -> str | None ; injected so it uses the same stealth stack
        self._fetch_text = fetch_text
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def _parser_for(self, url: str):
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if host in self._parsers:
                return self._parsers[host]
        robots_url = f"{host}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        try:
            text = self._fetch_text(robots_url)
            if text:
                rp.parse(text.splitlines())
            else:
                rp = None  # no robots.txt => allow all
        except Exception as e:
            log.debug("robots fetch failed for %s: %s", host, e)
            rp = None
        with self._lock:
            self._parsers[host] = rp
        return rp

    def allowed(self, url: str, user_agent: str = _UA) -> bool:
        rp = self._parser_for(url)
        if rp is None:
            return True
        try:
            return rp.can_fetch(user_agent, url)
        except Exception:
            return True

    def crawl_delay(self, url: str, user_agent: str = _UA) -> float | None:
        rp = self._parser_for(url)
        if rp is None:
            return None
        try:
            d = rp.crawl_delay(user_agent)
            return float(d) if d is not None else None
        except Exception:
            return None


class RateLimiter:
    """Minimum spacing between requests per domain (thread-safe)."""

    def __init__(self, per_sec: float) -> None:
        self.min_interval = 1.0 / per_sec if per_sec > 0 else 0.0
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str, extra_delay: float | None = None) -> None:
        domain = urlparse(url).netloc
        interval = max(self.min_interval, extra_delay or 0.0)
        if interval <= 0:
            return
        # Reserve this request's slot under the lock, then sleep OUTSIDE it. Holding
        # the lock across the sleep would serialize every domain; reserving the next
        # slot lets concurrent requests to the same domain queue up while different
        # domains proceed independently.
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last.get(domain, 0.0) + interval)
            self._last[domain] = slot
        sleep_for = slot - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
