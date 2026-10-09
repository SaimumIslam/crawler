"""Coherent, browser-realistic header sets.

The point is *consistency*: a Chrome User-Agent must ship with matching
`sec-ch-ua` client hints, Accept, and Accept-Language, in a plausible order.
Mismatched headers are themselves a bot signal.
"""
from __future__ import annotations

import random

# A small rotation of current, self-consistent desktop profiles.
_PROFILES = [
    {
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform": '"Windows"',
        "impersonate": "chrome",
    },
    {
        "ua": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "platform": '"macOS"',
        "impersonate": "chrome",
    },
    {
        "ua": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) "
            "Gecko/20100101 Firefox/132.0"
        ),
        "sec_ch_ua": None,  # Firefox does not send sec-ch-ua
        "platform": '"Windows"',
        "impersonate": "firefox",
    },
]


def pick_profile() -> dict:
    return random.choice(_PROFILES)


def headers_for(profile: dict, referer: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {
        "User-Agent": profile["ua"],
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": random.choice(
            ["en-US,en;q=0.9", "en-GB,en;q=0.8", "en-US,en;q=0.8"]
        ),
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none" if not referer else "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if profile.get("sec_ch_ua"):
        h["sec-ch-ua"] = profile["sec_ch_ua"]
        h["sec-ch-ua-mobile"] = "?0"
        h["sec-ch-ua-platform"] = profile["platform"]
    if referer:
        h["Referer"] = referer
    return h
