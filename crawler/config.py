"""Configuration: environment settings + per-site YAML template models.

A "template" is a declarative YAML file describing a crawl target: where to start,
which links to follow, what to extract, how politely/stealthily to fetch, and an
optional schedule. Templates are validated with pydantic so mistakes surface early.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()  # pull .env into os.environ if present

# --------------------------------------------------------------------------- #
# Global settings (from environment / .env)
# --------------------------------------------------------------------------- #

TEMPLATES_DIR = Path(
    os.getenv("CRAWLER_TEMPLATES_DIR") or str(Path(__file__).parent / "templates")
).resolve()

DATA_DIR = Path(os.getenv("CRAWLER_DATA_DIR") or "data").resolve()
DB_PATH = Path(os.getenv("CRAWLER_DB_PATH") or str(DATA_DIR / "crawler.db")).resolve()
EXPORT_DIR = Path(os.getenv("CRAWLER_EXPORT_DIR") or str(DATA_DIR / "exports")).resolve()
SESSION_DIR = Path(os.getenv("CRAWLER_SESSION_DIR") or str(DATA_DIR / "sessions")).resolve()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-fable-5")

# Proxies: comma-separated list in PROXY_POOL, or a single PROXY_URL.
# Format per entry: scheme://user:pass@host:port  (scheme http/https/socks5)
PROXY_POOL_RAW = os.getenv("PROXY_POOL", "") or os.getenv("PROXY_URL", "")


def ensure_dirs() -> None:
    for d in (DATA_DIR, EXPORT_DIR, SESSION_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Template models
# --------------------------------------------------------------------------- #

Tier = Literal["auto", "http", "patchright", "camoufox"]


class FieldSpec(BaseModel):
    """How to extract a single field from an item element."""

    css: str | None = None
    xpath: str | None = None
    regex: str | None = None
    attr: str | None = None  # convenience: extract this attribute
    default: Any = None
    many: bool = False  # collect a list instead of first match

    @model_validator(mode="after")
    def _need_a_source(self) -> "FieldSpec":
        if not (self.css or self.xpath):
            raise ValueError("field must define at least one of: css, xpath")
        return self


class FollowSpec(BaseModel):
    """Frontier rules: which links to enqueue and how deep to go."""

    link_selector: str | None = None  # CSS selector for links to follow
    link_xpath: str | None = None
    paginate: str | None = None  # CSS selector for the "next page" link
    max_depth: int = 1
    max_pages: int = 500  # hard safety cap on total pages per run


class ExtractSpec(BaseModel):
    mode: Literal["selectors", "llm"] = "selectors"
    item_selector: str | None = None  # CSS selecting each record container
    item_xpath: str | None = None
    fields: dict[str, FieldSpec] = Field(default_factory=dict)

    # LLM mode only:
    description: str | None = None  # natural-language "what to extract"
    infer_selectors: bool = False  # infer selectors once, cache into template

    @model_validator(mode="after")
    def _check_mode(self) -> "ExtractSpec":
        if self.mode == "selectors" and not self.fields:
            raise ValueError("selectors mode requires a non-empty 'fields' map")
        if self.mode == "llm" and not (self.fields or self.description):
            raise ValueError("llm mode requires 'description' and/or 'fields'")
        return self


class FetchSpec(BaseModel):
    min_tier: Tier = "auto"
    rate_limit_per_sec: float = 1.0  # max requests/sec to a single domain
    concurrency: int = 4
    respect_robots: bool = True
    timeout_sec: float = 30.0
    max_retries: int = 3
    impersonate: str = "chrome"  # curl_cffi impersonation target
    headless: bool = True
    render_wait_ms: int = 0  # extra wait for JS-heavy pages (browser tiers)


class CaptchaSpec(BaseModel):
    """How to behave when a CAPTCHA / human-verification challenge is encountered.

    This crawler does NOT auto-solve CAPTCHAs (no solving-farm or ML breaker). A
    CAPTCHA is the site's explicit "human required" gate; the only supported
    responses respect that:

      * skip   - (default, polite) log it, mark the URL blocked, move on.
      * manual - open a *headed* browser and pause so a human can solve it, then
                 continue with the resulting session. For sites you are authorized
                 to automate.
      * stop   - abort the whole run when a CAPTCHA is hit (strict).
    """

    on_detect: Literal["skip", "manual", "stop"] = "skip"
    manual_timeout_sec: float = 180.0  # how long to wait for a human in 'manual'


class ScheduleSpec(BaseModel):
    every: str | None = None  # "1d", "12h", "30m", "cron:..." or None
    at: str | None = None  # "HH:MM" for daily runs
    timezone: str = "UTC"


class SiteTemplate(BaseModel):
    name: str
    start_urls: list[str]
    allow_domains: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)  # regex on URL
    follow: FollowSpec = Field(default_factory=FollowSpec)
    extract: ExtractSpec
    fetch: FetchSpec = Field(default_factory=FetchSpec)
    captcha: CaptchaSpec = Field(default_factory=CaptchaSpec)
    schedule: ScheduleSpec = Field(default_factory=ScheduleSpec)

    @field_validator("start_urls")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("start_urls must not be empty")
        return v

    @model_validator(mode="after")
    def _default_allow_domains(self) -> "SiteTemplate":
        if not self.allow_domains:
            from urllib.parse import urlparse

            self.allow_domains = sorted(
                {urlparse(u).netloc for u in self.start_urls if urlparse(u).netloc}
            )
        return self

    # ----- loading helpers -------------------------------------------------- #

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SiteTemplate":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"template {path} did not parse to a mapping")
        return cls.model_validate(raw)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(exclude_none=True, exclude_defaults=True),
            sort_keys=False,
            allow_unicode=True,
        )
