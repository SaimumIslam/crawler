"""Fetcher interface and the common Response object shared by every tier."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class FetchResult:
    url: str
    status_code: int | None
    text: str
    tier: str
    ok: bool
    blocked: bool = False
    captcha: bool = False  # a human-verification CAPTCHA was present
    captcha_vendor: str | None = None
    final_url: str | None = None  # after redirects
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def html(self) -> str:
        return self.text


class Fetcher(Protocol):
    tier_name: str

    def fetch(self, url: str, proxy: str | None = None) -> FetchResult: ...

    def close(self) -> None: ...
