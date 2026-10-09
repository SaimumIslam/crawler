"""Proxy pool: rotation, sticky sessions, and ban eviction.

Works with an empty pool (direct connection). When PROXY_POOL / PROXY_URL is set,
`next_proxy()` round-robins healthy proxies; a proxy that produces repeated
failures/blocks is temporarily benched via `report_failure()`.
"""
from __future__ import annotations

import itertools
import threading
import time

from ..config import PROXY_POOL_RAW
from ..logging_conf import get_logger

log = get_logger("proxies")

_BENCH_SECONDS = 300  # how long a failing proxy sits out
_FAILS_TO_BENCH = 3


class ProxyPool:
    def __init__(self, raw: str | None = None) -> None:
        raw = PROXY_POOL_RAW if raw is None else raw
        self._all = [p.strip() for p in raw.split(",") if p.strip()]
        self._fails: dict[str, int] = {}
        self._benched_until: dict[str, float] = {}
        self._sticky: dict[str, str] = {}  # session_key -> proxy
        self._lock = threading.Lock()
        self._cycle = itertools.cycle(self._all) if self._all else None
        if self._all:
            log.info("Proxy pool loaded with %d proxies", len(self._all))
        else:
            log.info("No proxies configured; using direct connection")

    @property
    def enabled(self) -> bool:
        return bool(self._all)

    def _healthy(self) -> list[str]:
        now = time.time()
        return [p for p in self._all if self._benched_until.get(p, 0) <= now]

    def next_proxy(self, session_key: str | None = None) -> str | None:
        """Return a proxy URL (or None for direct). Sticky per session_key."""
        if not self._all:
            return None
        with self._lock:
            if session_key and session_key in self._sticky:
                p = self._sticky[session_key]
                if self._benched_until.get(p, 0) <= time.time():
                    return p
                # sticky proxy got benched; pick a fresh one
                self._sticky.pop(session_key, None)

            healthy = self._healthy()
            if not healthy:
                # everything benched; clear the oldest bench and reuse
                soonest = min(self._benched_until, key=self._benched_until.get)
                self._benched_until.pop(soonest, None)
                healthy = [soonest]

            # advance the round-robin cursor to the next healthy proxy
            chosen = None
            for _ in range(len(self._all)):
                cand = next(self._cycle)  # type: ignore[arg-type]
                if cand in healthy:
                    chosen = cand
                    break
            chosen = chosen or healthy[0]
            if session_key:
                self._sticky[session_key] = chosen
            return chosen

    def report_success(self, proxy: str | None) -> None:
        if proxy:
            with self._lock:
                self._fails.pop(proxy, None)

    def report_failure(self, proxy: str | None) -> None:
        if not proxy:
            return
        with self._lock:
            self._fails[proxy] = self._fails.get(proxy, 0) + 1
            if self._fails[proxy] >= _FAILS_TO_BENCH:
                self._benched_until[proxy] = time.time() + _BENCH_SECONDS
                self._fails[proxy] = 0
                log.warning("Benched proxy %s for %ds", proxy, _BENCH_SECONDS)
