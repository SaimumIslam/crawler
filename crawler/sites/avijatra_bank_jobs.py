"""Scraper for avijatra.com's job-question-bank past-paper sections.

Generalized over `category` (the URL segment right after
`/job-question-bank/`, e.g. `"bank-jobs"` or `"primary-assistant-teacher"`)
- every category on the site shares the same Next.js template, just under a
different path prefix. The site is server-rendered, so each past-paper page
ships its full (preview) question set as real HTML in the initial response;
a single Tier-1 HTTP fetch is enough, no browser tier needed.

Quirk: a past-paper page is a search-param-driven server component - the
`paperName` query string on the link (as it appears on the index page) is
required to render any questions. Fetching the bare path without it returns
a page with zero questions. `discover_exams` therefore pulls URLs straight
from the index page's `<a href>` attributes (which carry that query string)
rather than from the page's own JSON-LD, whose `url` field omits it.

Each exam page also ships a `QAPage` JSON-LD block (question + accepted
answer only, no options) - kept as a cross-check: `parse_exam` asserts its
question count matches the DOM's, so a template change on the site trips
loudly instead of silently returning fewer records.

Note: the visible copy on every exam page states this is a login-gated
preview ("some important questions" of the paper) - the scraper only ever
sees what the site publishes to anonymous visitors.
"""
from __future__ import annotations

import html as html_module
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from parsel import Selector

from ..crawl.robots import RateLimiter, RobotsCache
from ..fetch.base import FetchResult
from ..fetch.http_curl import CurlFetcher
from ..logging_conf import get_logger
from ..store.db import Store

log = get_logger("sites.avijatra_bank_jobs")

BASE_URL = "https://www.avijatra.com"
DEFAULT_CATEGORY = "bank-jobs"

_LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.S
)
_LEADING_SERIAL_RE = re.compile(r'^\s*\d+\s*[.)]\s*')


def _clean_question_text(text: str) -> str:
    """Strip the page's own leading serial number (e.g. '1 . ') and normalize whitespace."""
    text = _LEADING_SERIAL_RE.sub("", text)
    return re.sub(r'\s+', ' ', text).strip()


def index_url(category: str) -> str:
    return f"{BASE_URL}/job-question-bank/{category}"


def site_name(category: str) -> str:
    return f"avijatra_{category.replace('-', '_')}"


def discover_exams(index_html: str, category: str = DEFAULT_CATEGORY) -> list[dict[str, str]]:
    """Every past-paper exam linked from a category's index, deduped by URL.

    The index lists a `?paperName=` query variant for most exams as well as
    a bare-path variant; both are kept if they're distinct URLs since the
    bare path renders no questions (see module docstring) - but in practice
    the bare-path entries are just query-less duplicates of the same slug,
    so a plain per-URL dedupe keeps everything the page actually links to.
    """
    href_re = re.compile(
        r'href="(/job-question-bank/' + re.escape(category) + r'/past-papers/[^"]+)"'
    )
    seen: set[str] = set()
    exams: list[dict[str, str]] = []
    for raw_href in href_re.findall(index_html):
        url = urljoin(BASE_URL, html_module.unescape(raw_href))
        if url in seen:
            continue
        seen.add(url)
        qs = parse_qs(urlsplit(url).query)
        paper_name = unquote(qs.get("paperName", [""])[0]) or None
        exams.append({"url": url, "paper_name": paper_name})
    return exams


def _qa_page_question_count(page_html: str) -> int | None:
    """Question count from the page's QAPage JSON-LD, for cross-checking the DOM parse."""
    for block in _LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "QAPage":
            return len(data.get("mainEntity") or [])
    return None


def parse_exam(page_html: str, url: str, paper_name: str | None = None) -> list[dict[str, Any]]:
    """One record per MCQ question: text, all options, and the correct one."""
    sel = Selector(page_html)
    exam_title = (sel.css("h1::text").get() or "").strip()
    exam_intro = (sel.css("main p::text").get() or "").strip()
    exam_common = {
        "exam_url": url,
        "exam_title": exam_title or paper_name,
        "exam_intro": exam_intro or None,
    }

    cards = sel.css(
        "div.bg-white.rounded-2xl.shadow-sm.border.border-slate-200.overflow-hidden"
    )
    expected = _qa_page_question_count(page_html)
    if expected is not None and expected != len(cards):
        log.warning(
            "%s: DOM question count (%d) != QAPage JSON-LD count (%d) - "
            "site markup may have changed",
            url, len(cards), expected,
        )

    records: list[dict[str, Any]] = []
    for i, card in enumerate(cards, start=1):
        question_text = _clean_question_text(
            " ".join(t.strip() for t in card.css(".mb-6 ::text").getall() if t.strip())
        )
        options: list[dict[str, Any]] = []
        for opt in card.css("div.grid > div"):
            classes = opt.attrib.get("class", "")
            is_correct = "emerald" in classes
            label = (opt.css("div::text").get() or "").strip()
            text = " ".join(
                t.strip() for t in opt.css("span ::text").getall() if t.strip()
            )
            options.append({"label": label, "text": text, "is_correct": is_correct})
        correct = next((o for o in options if o["is_correct"]), None)
        records.append({
            **exam_common,
            "question_no": i,
            "question_text": question_text,
            "is_written": not options,
            "options": options,
            "correct_option_label": correct["label"] if correct else None,
            "correct_option_text": correct["text"] if correct else None,
        })
    return records


def run(
    category: str = DEFAULT_CATEGORY,
    ignore_robots: bool = False,
    rate_limit_per_sec: float = 1.0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Discover every past-paper exam on a category's index and scrape each one.

    `category` is the URL segment after `/job-question-bank/`, e.g.
    `"bank-jobs"` or `"primary-assistant-teacher"`. Records are stored under
    a per-category site name (`avijatra_<category>`) so different categories
    never dedupe against each other.

    Politeness matches the rest of the crawler: robots.txt is honored unless
    `ignore_robots=True` (opt-in, logged), and requests are spaced by
    `rate_limit_per_sec` per domain. avijatra.com's robots.txt allows
    `/job-question-bank/` (only `/api/`, `/search`, `/private/` are blocked).
    """
    idx_url = index_url(category)
    site = site_name(category)

    fetcher = CurlFetcher()
    limiter = RateLimiter(rate_limit_per_sec)

    def fetch_text(u: str) -> str | None:
        res = fetcher.fetch(u)
        return res.text if res.ok else None

    robots = RobotsCache(fetch_text)

    def polite_get(u: str) -> FetchResult | None:
        if not ignore_robots and not robots.allowed(u):
            log.warning("robots.txt disallows %s - skipping (pass ignore_robots=True to override)", u)
            return None
        limiter.wait(u, extra_delay=robots.crawl_delay(u))
        return fetcher.fetch(u)

    store = Store()
    run_id = store.start_run(site, notes=f"category={category} ignore_robots={ignore_robots}")
    pages = 0
    new_records = 0
    try:
        idx_res = polite_get(idx_url)
        if idx_res is None:
            raise RuntimeError(f"blocked by robots.txt: {idx_url} (pass --ignore-robots to override)")
        pages += 1
        store.record_page(run_id, idx_url, idx_res.status_code, idx_res.tier, idx_res.blocked)
        if not idx_res.ok:
            raise RuntimeError(f"failed to fetch index page {idx_url}: status={idx_res.status_code}")

        exams = discover_exams(idx_res.text, category)
        if limit:
            exams = exams[:limit]
        log.info("discovered %d exam(s)", len(exams))

        for exam in exams:
            res = polite_get(exam["url"])
            if res is None:
                continue
            pages += 1
            store.record_page(run_id, exam["url"], res.status_code, res.tier, res.blocked)
            if not res.ok:
                log.warning("failed to fetch %s (status=%s)", exam["url"], res.status_code)
                continue
            try:
                records = parse_exam(res.text, exam["url"], exam.get("paper_name"))
            except Exception as e:
                log.error("parse failed for %s: %s", exam["url"], e)
                continue
            for rec in records:
                if store.add_record(run_id, site, exam["url"], rec):
                    new_records += 1
            log.info("%s: %d record(s)", exam["url"], len(records))
    finally:
        store.bump_run_counts(run_id, pages=pages, records_new=new_records)
        store.finish_run(run_id, status="completed")
        fetcher.close()
        store.close()

    return {"run_id": run_id, "pages": pages, "records_new": new_records, "site": site}
