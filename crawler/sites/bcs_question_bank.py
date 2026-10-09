"""Scraper for bcsconfidence.online's BCS exam question bank.

The site is an Inertia.js app: every page ships its full component props as a
JSON blob inside `<script data-page="app" type="application/json">`, so a
single Tier-1 HTTP fetch is enough - no JS rendering needed.

Two exam kinds live under /bcs/question-bank/<kind>/<slug>:

  - preli    (`Guest/Resources/QuestionBank/Preliminary`) - MCQ exams. Full
    per-question data is in the payload: `question_bank` (exam metadata) and
    `questions_by_subject` (subject -> question -> head/body/answer).
  - written  (`Guest/Resources/QuestionBank/Written`) - essay exams. Questions
    are scanned PDF files (`question_sets[].question_file_url`), not
    structured text, so only exam + per-subject file metadata is scraped.

`robots.txt` on this domain is `Disallow: /` - this module respects that by
default (`ignore_robots=False`). Only pass `ignore_robots=True` for a target
you are authorized to crawl; the run is logged either way.
"""
from __future__ import annotations

import html as html_module
import json
import re
from typing import Any
from urllib.parse import urljoin

from ..crawl.robots import RateLimiter, RobotsCache
from ..fetch.base import FetchResult
from ..fetch.http_curl import CurlFetcher
from ..logging_conf import get_logger
from ..store.db import Store

log = get_logger("sites.bcs_question_bank")

BASE_URL = "https://bcsconfidence.online"
INDEX_URL = f"{BASE_URL}/bcs/question-bank"
SITE_NAME = "bcs_question_bank"

_PAGE_JSON_RE = re.compile(
    r'data-page="app"\s+type="application/json">(.*?)</script>', re.S
)


def _extract_page_json(page_html: str) -> dict[str, Any]:
    m = _PAGE_JSON_RE.search(page_html)
    if not m:
        raise ValueError("no Inertia data-page payload found on page")
    return json.loads(html_module.unescape(m.group(1)))


def discover_exams(index_html: str) -> list[dict[str, str]]:
    """Return every exam linked from the question-bank index page (deduped by URL -
    the index JSON lists some exams more than once, e.g. across UI filter groups)."""
    props = _extract_page_json(index_html)["props"]
    exams: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for group_key, kind in (("preli_questions", "preli"), ("written_questions", "written")):
        for item in props.get(group_key) or []:
            url = f"{INDEX_URL}/{kind}/{item['slug']}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            exams.append({"exam_kind": kind, "slug": item["slug"], "url": url})
    return exams


def _exam_common_fields(exam: dict[str, Any], url: str) -> dict[str, Any]:
    return {
        "exam_id": exam.get("id"),
        "exam_slug": exam.get("slug"),
        "exam_name": exam.get("name"),
        "exam_type": exam.get("exam_type"),  # "Preli" | "Written"
        "exam_category": exam.get("type"),  # "General" | "Health" | ...
        "exam_date": exam.get("date_format") or exam.get("date"),
        "duration_min": exam.get("duration"),
        "total_mark": exam.get("total_mark"),
        "marking": exam.get("marking"),
        "negative_marking": exam.get("negative_marking"),
        "number_of_questions": exam.get("number_of_questions"),
        "number_of_options": exam.get("number_of_options"),
        "exam_url": url,
    }


def parse_preli_exam(page_html: str, url: str) -> list[dict[str, Any]]:
    """One record per MCQ question: text, options, correct answer, subject, exam meta."""
    props = _extract_page_json(page_html)["props"]
    exam_common = _exam_common_fields(props.get("question_bank") or {}, url)
    records: list[dict[str, Any]] = []
    for subj in props.get("questions_by_subject") or []:
        subj_common = {
            "subject_id": subj.get("subject_id"),
            "subject_name": subj.get("subject_name"),
            "subject_slug": subj.get("subject_slug"),
        }
        for idx, q in enumerate(subj.get("questions") or [], start=1):
            head = q.get("head") or {}
            options = [
                {
                    "position": b.get("position"),
                    "serial": b.get("serial"),
                    "text": b.get("data"),
                    "is_image": b.get("is_image"),
                    "is_correct": b.get("is_correct"),
                }
                for b in q.get("body") or []
            ]
            correct = next((o for o in options if o["is_correct"]), None)
            records.append({
                **exam_common,
                **subj_common,
                "question_id": q.get("id"),
                "question_no": idx,
                "question_serial": head.get("serial"),
                "question_text": head.get("data"),
                "question_is_image": head.get("is_image"),
                "options": options,
                "answer_position": q.get("answer"),
                "correct_option_text": correct["text"] if correct else None,
                "correct_option_serial": correct["serial"] if correct else None,
            })
    return records


def parse_written_exam(page_html: str, url: str) -> list[dict[str, Any]]:
    """One record per subject's question paper (a PDF - no per-question text exists)."""
    exam = _extract_page_json(page_html)["props"].get("question_bank") or {}
    exam_common = _exam_common_fields(exam, url)
    records: list[dict[str, Any]] = []
    for s in exam.get("question_sets") or []:
        file_url = s.get("question_file_url") or (
            urljoin(BASE_URL, s["question_file"]) if s.get("question_file") else None
        )
        records.append({
            **exam_common,
            "subject_id": s.get("subject_id"),
            "question_set_id": s.get("id"),
            "question_lang": s.get("question_lang"),
            "question_file_url": file_url,
            "answer_file": s.get("answer_file") or None,
            "note": "written-exam questions are a scanned PDF; the site exposes no "
                    "structured per-question text for this exam kind",
        })
    return records


def run(
    ignore_robots: bool = False,
    rate_limit_per_sec: float = 1.0,
    limit: int | None = None,
    only: str | None = None,  # "preli" | "written" | None (both)
) -> dict[str, Any]:
    """Discover every exam on the question-bank index and scrape each one.

    Politeness matches the rest of the crawler: robots.txt is honored unless
    `ignore_robots=True` (opt-in, logged), and requests are spaced by
    `rate_limit_per_sec` per domain.
    """
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
    run_id = store.start_run(SITE_NAME, notes=f"ignore_robots={ignore_robots}")
    pages = 0
    new_records = 0
    try:
        idx_res = polite_get(INDEX_URL)
        if idx_res is None:
            raise RuntimeError(f"blocked by robots.txt: {INDEX_URL} (pass --ignore-robots to override)")
        pages += 1
        store.record_page(run_id, INDEX_URL, idx_res.status_code, idx_res.tier, idx_res.blocked)
        if not idx_res.ok:
            raise RuntimeError(f"failed to fetch index page {INDEX_URL}: status={idx_res.status_code}")

        exams = discover_exams(idx_res.text)
        if only:
            exams = [e for e in exams if e["exam_kind"] == only]
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
                parse = parse_preli_exam if exam["exam_kind"] == "preli" else parse_written_exam
                records = parse(res.text, exam["url"])
            except Exception as e:
                log.error("parse failed for %s: %s", exam["url"], e)
                continue
            for rec in records:
                if store.add_record(run_id, SITE_NAME, exam["url"], rec):
                    new_records += 1
            log.info("%s: %d record(s)", exam["url"], len(records))
    finally:
        store.bump_run_counts(run_id, pages=pages, records_new=new_records)
        store.finish_run(run_id, status="completed")
        fetcher.close()
        store.close()

    return {"run_id": run_id, "pages": pages, "records_new": new_records}
