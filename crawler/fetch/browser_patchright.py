"""Tier 2 fetcher: Patchright (undetected Playwright fork), real Chrome channel.

Patchright patches the CDP/automation-protocol leaks that make vanilla Playwright
detectable. We drive it synchronously (sync API) so it slots into the same
Fetcher interface as the other tiers. Imported lazily so the package works even
when Patchright / its browser binaries are not installed.
"""
from __future__ import annotations

from ..logging_conf import get_logger
from .base import FetchResult
from .behavior import human_page_actions, wait_for_clearance, wait_for_manual_solve
from .blockdetect import captcha_vendor, is_blocked, is_challenge_interstitial
from .headers import pick_profile
from .session import storage_state_path

log = get_logger("fetch.patchright")


class PatchrightFetcher:
    tier_name = "patchright"

    def __init__(
        self,
        timeout: float = 30.0,
        headless: bool = True,
        render_wait_ms: int = 0,
        site: str = "default",
        persist_session: bool = True,
        captcha_mode: str = "skip",
        manual_timeout_sec: float = 180.0,
    ) -> None:
        self.timeout_ms = int(timeout * 1000)
        self.headless = headless
        self.render_wait_ms = render_wait_ms
        self.site = site
        self.persist_session = persist_session
        self.captcha_mode = captcha_mode
        self.manual_timeout_ms = int(manual_timeout_sec * 1000)
        self._pw = None
        self._browser = None

    def _ensure_browser(self):
        if self._browser is not None:
            return
        from patchright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Prefer the installed Google Chrome (real TLS profile); fall back to the
        # bundled Chromium if Chrome isn't present on the machine.
        try:
            self._browser = self._pw.chromium.launch(
                headless=self.headless, channel="chrome"
            )
        except Exception as e:
            log.info("Chrome channel unavailable (%s); using bundled Chromium", e)
            self._browser = self._pw.chromium.launch(headless=self.headless)

    def fetch(self, url: str, proxy: str | None = None) -> FetchResult:
        try:
            self._ensure_browser()
        except Exception as e:
            log.warning("Patchright unavailable: %s", e)
            return FetchResult(url, None, "", self.tier_name, ok=False, error=str(e))

        profile = pick_profile()
        state_path = storage_state_path(self.site) if self.persist_session else None
        ctx_kwargs: dict = {
            "user_agent": profile["ua"],
            "locale": "en-US",
            "viewport": {"width": 1366, "height": 768},
        }
        if proxy:
            ctx_kwargs["proxy"] = {"server": proxy}
        if state_path and state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)

        context = self._browser.new_context(**ctx_kwargs)
        try:
            page = context.new_page()
            resp = page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
            human_page_actions(page, self.render_wait_ms)
            status = resp.status if resp else None
            text = page.content()
            # If we landed on a challenge interstitial, wait for it to clear.
            if is_challenge_interstitial(text):
                text = wait_for_clearance(page, timeout_ms=self.timeout_ms)
                if not is_challenge_interstitial(text):
                    status = 200  # challenge cleared -> real content served
            # CAPTCHA handling per configured policy (never auto-solved).
            vendor = captcha_vendor(text)
            if vendor and self.captcha_mode == "manual" and not self.headless:
                text = wait_for_manual_solve(page, self.manual_timeout_ms, url)
                vendor = captcha_vendor(text)
                if vendor is None:
                    status = 200
            blocked = is_blocked(status, text)
            ok = (status is not None and 200 <= status < 300) and not blocked and vendor is None
            if state_path:
                try:
                    context.storage_state(path=str(state_path))
                except Exception:
                    pass
            return FetchResult(
                url=url, status_code=status, text=text, tier=self.tier_name,
                ok=ok, blocked=blocked, captcha=vendor is not None,
                captcha_vendor=vendor, final_url=page.url,
            )
        except Exception as e:
            log.debug("Patchright error for %s: %s", url, e)
            return FetchResult(url, None, "", self.tier_name, ok=False, error=str(e))
        finally:
            context.close()

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
