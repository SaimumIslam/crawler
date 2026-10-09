"""Tier 3 fetcher: Camoufox (hardened Firefox) for the hardest JS-fingerprint walls.

Camoufox spoofs canvas/WebGL/fonts/navigator at the engine (C++) level rather than
via JS injection, so it defeats fingerprinting that catches patched-Chromium tiers.
Slowest and heaviest; used only when lower tiers fail. Lazily imported.
"""
from __future__ import annotations

from ..logging_conf import get_logger
from .base import FetchResult
from .behavior import human_page_actions, wait_for_clearance, wait_for_manual_solve
from .blockdetect import captcha_vendor, is_blocked, is_challenge_interstitial

log = get_logger("fetch.camoufox")


class CamoufoxFetcher:
    tier_name = "camoufox"

    def __init__(
        self,
        timeout: float = 40.0,
        headless: bool = True,
        render_wait_ms: int = 0,
        captcha_mode: str = "skip",
        manual_timeout_sec: float = 180.0,
    ) -> None:
        self.timeout_ms = int(timeout * 1000)
        self.headless = headless
        self.render_wait_ms = render_wait_ms
        self.captcha_mode = captcha_mode
        self.manual_timeout_ms = int(manual_timeout_sec * 1000)
        self._cm = None
        self._browser = None

    def _ensure_browser(self, proxy: str | None):
        if self._browser is not None:
            return
        from camoufox.sync_api import Camoufox

        kwargs: dict = {"headless": self.headless, "humanize": True}
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        # Camoufox() is a context manager yielding a Playwright-Firefox browser.
        self._cm = Camoufox(**kwargs)
        self._browser = self._cm.__enter__()

    def fetch(self, url: str, proxy: str | None = None) -> FetchResult:
        try:
            self._ensure_browser(proxy)
        except Exception as e:
            log.warning("Camoufox unavailable: %s", e)
            return FetchResult(url, None, "", self.tier_name, ok=False, error=str(e))

        try:
            page = self._browser.new_page()
            resp = page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            human_page_actions(page, self.render_wait_ms)
            status = resp.status if resp else None
            text = page.content()
            if is_challenge_interstitial(text):
                text = wait_for_clearance(page, timeout_ms=self.timeout_ms)
                if not is_challenge_interstitial(text):
                    status = 200
            vendor = captcha_vendor(text)
            if vendor and self.captcha_mode == "manual" and not self.headless:
                text = wait_for_manual_solve(page, self.manual_timeout_ms, url)
                vendor = captcha_vendor(text)
                if vendor is None:
                    status = 200
            blocked = is_blocked(status, text)
            ok = (status is not None and 200 <= status < 300) and not blocked and vendor is None
            final = page.url
            page.close()
            return FetchResult(
                url=url, status_code=status, text=text, tier=self.tier_name,
                ok=ok, blocked=blocked, captcha=vendor is not None,
                captcha_vendor=vendor, final_url=final,
            )
        except Exception as e:
            log.debug("Camoufox error for %s: %s", url, e)
            return FetchResult(url, None, "", self.tier_name, ok=False, error=str(e))

    def close(self) -> None:
        try:
            if self._cm is not None:
                self._cm.__exit__(None, None, None)
        except Exception:
            pass
        self._cm = None
        self._browser = None
