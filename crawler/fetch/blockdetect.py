"""Detect anti-bot challenge / block pages.

A "soft block" is an HTTP 200 whose body is actually a challenge (Cloudflare
"Just a moment...", Akamai/DataDome/PerimeterX interstitials, etc.). These must be
detected from content, not just status code, so the escalation logic can bump the
request to a heavier tier.

Two marker classes, to avoid false positives on full, legitimate pages:
  * STRONG markers are specific enough to a challenge interstitial that they block
    regardless of page size (e.g. Cloudflare's cf_chl tokens, DataDome captcha).
  * WEAK markers (generic phrases like "access denied") only count when the page is
    short or the phrase appears in the <title> — otherwise ordinary content that
    merely mentions the phrase would be misclassified as a block.
"""
from __future__ import annotations

import re

_BLOCK_STATUS = {401, 403, 407, 429, 503}

# Specific to challenge/interstitial pages — safe to trigger at any length.
_STRONG = [
    re.compile(r"cf-browser-verification|cf_chl_|__cf_chl|/cdn-cgi/challenge-platform", re.I),
    re.compile(r"just a moment\.\.\.", re.I),
    re.compile(r"checking your browser before", re.I),
    re.compile(r"enable javascript and cookies to continue", re.I),
    re.compile(r"datadome|px-captcha|perimeterx", re.I),
    re.compile(r"_pxhd|_pxvid|px\.captcha", re.I),
    re.compile(r"attention required.{0,40}cloudflare", re.I),
    re.compile(r"request unsuccessful.*incapsula", re.I),
]

# Generic phrases — only count on a short body or inside <title>.
_WEAK = [
    re.compile(r"access denied", re.I),
    re.compile(r"are you a human|verify you are human", re.I),
    re.compile(r"unusual traffic", re.I),
    re.compile(r"<title>\s*403\s*forbidden", re.I),
]

_SHORT_BODY = 2000  # bodies below this are likely interstitials, not real content


def _title(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    return m.group(1) if m else ""


# Named CAPTCHA / human-verification widgets. Detecting one means "a human is
# required here" — we never auto-solve; the CaptchaSpec mode decides what to do.
_CAPTCHA_VENDORS = [
    ("recaptcha", re.compile(r"g-recaptcha|recaptcha/api|www\.google\.com/recaptcha|grecaptcha\.", re.I)),
    ("hcaptcha", re.compile(r"h-captcha|hcaptcha\.com|js\.hcaptcha\.com", re.I)),
    ("turnstile", re.compile(r"cf-turnstile|challenges\.cloudflare\.com/turnstile", re.I)),
    ("arkose", re.compile(r"funcaptcha|arkoselabs|arkose\.com", re.I)),
    ("image-text", re.compile(r"<img[^>]+captcha|name=[\"']captcha[\"']|id=[\"']captcha[\"']", re.I)),
]


def captcha_vendor(text: str) -> str | None:
    """Return the name of a detected CAPTCHA widget, or None."""
    if not text:
        return None
    head = text[:30000]
    for name, pat in _CAPTCHA_VENDORS:
        if pat.search(head):
            return name
    return None


def is_captcha(text: str) -> bool:
    return captcha_vendor(text) is not None


def is_challenge_interstitial(text: str) -> bool:
    """True if the HTML is an active challenge interstitial (not merely a page that
    references a bot vendor). Used by the browser tiers to know when to keep waiting
    for the challenge to auto-solve."""
    if not text:
        return False
    head = text[:20000]
    if re.search(r"<title[^>]*>\s*just a moment", head, re.I):
        return True
    for pat in _STRONG:
        if pat.search(head):
            return True
    return False


def is_blocked(status_code: int | None, text: str) -> bool:
    if status_code in _BLOCK_STATUS:
        return True
    if not text:
        return False
    head = text[:20000]  # markers appear near the top
    for pat in _STRONG:
        if pat.search(head):
            return True
    short = len(text.strip()) < _SHORT_BODY
    title = _title(head)
    for pat in _WEAK:
        if pat.search(title) or (short and pat.search(head)):
            return True
    return False
