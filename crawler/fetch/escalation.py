"""Tiered fetcher: escalate cheap -> expensive only as the target forces it.

Order: http (curl_cffi) -> patchright -> camoufox.
A block or hard error at one tier bumps the URL to the next and rotates the proxy.
Within a tier, transient errors are retried with capped exponential backoff.

`min_tier` from the template pins the starting tier ("auto" starts at http).
Browser tiers are constructed lazily and reused across a run, then closed.
"""
from __future__ import annotations

import random
import time

from ..config import CaptchaSpec, FetchSpec
from ..logging_conf import get_logger
from .base import FetchResult
from .http_curl import CurlFetcher
from .proxies import ProxyPool

log = get_logger("fetch.escalation")

_TIER_ORDER = ["http", "patchright", "camoufox"]


class TieredFetcher:
    def __init__(
        self,
        spec: FetchSpec,
        site: str = "default",
        proxies: ProxyPool | None = None,
        captcha: CaptchaSpec | None = None,
    ) -> None:
        self.spec = spec
        self.site = site
        self.proxies = proxies or ProxyPool()
        self.captcha = captcha or CaptchaSpec()
        self._fetchers: dict[str, object] = {}

        start = spec.min_tier if spec.min_tier != "auto" else "http"
        self._start_index = _TIER_ORDER.index(start)
        # Manual CAPTCHA solving needs a visible window, so a browser tier must run
        # headed. Also ensure we can actually reach a browser tier to solve.
        self._browser_headless = spec.headless and self.captcha.on_detect != "manual"

    # ----- lazy tier construction ------------------------------------------ #

    def _get_fetcher(self, tier: str):
        if tier in self._fetchers:
            return self._fetchers[tier]
        if tier == "http":
            f = CurlFetcher(self.spec.timeout_sec, self.spec.impersonate)
        elif tier == "patchright":
            from .browser_patchright import PatchrightFetcher

            f = PatchrightFetcher(
                timeout=self.spec.timeout_sec,
                headless=self._browser_headless,
                render_wait_ms=self.spec.render_wait_ms,
                site=self.site,
                captcha_mode=self.captcha.on_detect,
                manual_timeout_sec=self.captcha.manual_timeout_sec,
            )
        elif tier == "camoufox":
            from .browser_camoufox import CamoufoxFetcher

            f = CamoufoxFetcher(
                timeout=self.spec.timeout_sec,
                headless=self._browser_headless,
                render_wait_ms=self.spec.render_wait_ms,
                captcha_mode=self.captcha.on_detect,
                manual_timeout_sec=self.captcha.manual_timeout_sec,
            )
        else:
            raise ValueError(f"unknown tier {tier}")
        self._fetchers[tier] = f
        return f

    # ----- main entry ------------------------------------------------------- #

    def fetch(self, url: str) -> FetchResult:
        last: FetchResult | None = None
        captcha_vendor_seen: str | None = None
        for tier in _TIER_ORDER[self._start_index :]:
            result = self._fetch_with_retries(tier, url)
            last = result
            if result.ok:
                return result
            if result.captcha:
                captcha_vendor_seen = result.captcha_vendor or captcha_vendor_seen
                # In 'manual' mode a human already had a chance in a visible window at
                # this browser tier; opening another window at the next tier won't help.
                if self.captcha.on_detect == "manual" and tier in ("patchright", "camoufox"):
                    log.info("Manual CAPTCHA attempt did not clear at %s on %s; "
                             "not opening another browser", tier, url)
                    break
                # Otherwise escalate http -> browser so 'manual' can run at all, or so
                # a heavier tier can attempt the challenge.
                log.info("Tier %s hit %s CAPTCHA on %s; escalating",
                         tier, result.captcha_vendor, url)
                continue
            if result.blocked:
                log.info("Tier %s blocked on %s; escalating", tier, url)
                continue
            # hard error (no response): try next tier too
            log.debug("Tier %s failed on %s (%s); escalating", tier, url, result.error)
        # No tier succeeded. If any tier saw a CAPTCHA, surface that on the final
        # result so the engine's skip/stop policy fires correctly regardless of how
        # the last tier happened to render the page.
        if last is not None and not last.ok and captcha_vendor_seen and not last.captcha:
            last.captcha = True
            last.captcha_vendor = captcha_vendor_seen
        return last or FetchResult(url, None, "", "http", ok=False, error="all tiers failed")

    def _fetch_with_retries(self, tier: str, url: str) -> FetchResult:
        fetcher = self._get_fetcher(tier)
        session_key = f"{self.site}:{tier}"
        attempts = max(1, self.spec.max_retries)
        result: FetchResult | None = None
        for attempt in range(attempts):
            proxy = self.proxies.next_proxy(session_key=session_key)
            result = fetcher.fetch(url, proxy=proxy)  # type: ignore[attr-defined]
            if result.ok:
                self.proxies.report_success(proxy)
                return result
            if result.blocked or result.error:
                self.proxies.report_failure(proxy)
            # Do not waste retries on a clean block or a CAPTCHA — escalate instead.
            if result.blocked or result.captcha:
                return result
            if attempt < attempts - 1:
                backoff = min(30.0, (2**attempt)) + random.uniform(0, 0.75)
                time.sleep(backoff)
        return result  # type: ignore[return-value]

    def close(self) -> None:
        for f in self._fetchers.values():
            try:
                f.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._fetchers.clear()

    def __enter__(self) -> "TieredFetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
