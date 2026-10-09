"""Tier 1 fetcher: curl_cffi with browser TLS/JA3-JA4 + HTTP/2 impersonation.

This is the cheapest tier and defeats fingerprinting that gates on the TLS
handshake and HTTP/2 frame ordering — the layer plain `requests`/`httpx` fail.
"""
from __future__ import annotations

from ..logging_conf import get_logger
from .base import FetchResult
from .blockdetect import captcha_vendor, is_blocked
from .headers import headers_for, pick_profile

log = get_logger("fetch.http")


class CurlFetcher:
    tier_name = "http"

    def __init__(self, timeout: float = 30.0, impersonate: str = "chrome") -> None:
        self.timeout = timeout
        self.default_impersonate = impersonate

    def fetch(self, url: str, proxy: str | None = None) -> FetchResult:
        from curl_cffi import requests as cffi  # imported lazily

        profile = pick_profile()
        impersonate = profile.get("impersonate", self.default_impersonate)
        headers = headers_for(profile)
        proxies = {"http": proxy, "https": proxy} if proxy else None
        try:
            resp = cffi.get(
                url,
                headers=headers,
                impersonate=impersonate,
                proxies=proxies,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except Exception as e:  # network/proxy/timeout errors
            log.debug("Tier1 error for %s: %s", url, e)
            return FetchResult(
                url=url, status_code=None, text="", tier=self.tier_name,
                ok=False, blocked=False, error=str(e),
            )

        text = resp.text or ""
        blocked = is_blocked(resp.status_code, text)
        vendor = captcha_vendor(text)
        # A CAPTCHA can't be handled at the HTTP tier — flag it so escalation moves
        # to a browser tier where skip/manual policy applies.
        ok = (200 <= resp.status_code < 300) and not blocked and vendor is None
        return FetchResult(
            url=url,
            status_code=resp.status_code,
            text=text,
            tier=self.tier_name,
            ok=ok,
            blocked=blocked,
            captcha=vendor is not None,
            captcha_vendor=vendor,
            final_url=str(resp.url),
            headers=dict(resp.headers),
        )

    def close(self) -> None:  # nothing persistent to close
        pass
