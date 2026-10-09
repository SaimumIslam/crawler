"""Selector-based extraction using parsel (CSS + XPath + regex).

Supports the pseudo-element conventions common in Scrapy-style selectors:
  - `a::text`         -> text content
  - `a::attr(href)`   -> attribute value
A `FieldSpec.regex` post-filters the matched string(s); `many: true` returns lists.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from parsel import Selector, SelectorList

from ..config import ExtractSpec, FieldSpec


def _apply_regex(value: str, pattern: str | None) -> str | None:
    if value is None:
        return None
    if not pattern:
        return value
    m = re.search(pattern, value)
    if not m:
        return None
    # If the pattern has a capture group, prefer it.
    return m.group(1) if m.groups() else m.group(0)


def _extract_field(node: Selector, spec: FieldSpec, base_url: str | None) -> Any:
    sel: SelectorList
    if spec.css:
        sel = node.css(spec.css)
    elif spec.xpath:
        sel = node.xpath(spec.xpath)
    else:
        return spec.default

    # If an explicit attr is requested and the selector didn't already use ::attr
    if spec.attr and "::" not in (spec.css or ""):
        sel = sel.xpath(f"@{spec.attr}")

    values = sel.getall()
    # Strip whitespace, drop empties.
    values = [v.strip() for v in values if v is not None and v.strip()]

    # Resolve relative URLs for url-ish fields when a base is available.
    if base_url:
        values = [
            urljoin(base_url, v) if _looks_like_url_attr(spec) else v for v in values
        ]

    if spec.regex:
        values = [r for v in values if (r := _apply_regex(v, spec.regex)) is not None]

    if spec.many:
        return values or (spec.default if spec.default is not None else [])
    return values[0] if values else spec.default


def _looks_like_url_attr(spec: FieldSpec) -> bool:
    src = (spec.css or spec.xpath or "").lower()
    return "attr(href)" in src or "attr(src)" in src or spec.attr in ("href", "src")


def extract_records(
    html: str, spec: ExtractSpec, base_url: str | None = None
) -> list[dict[str, Any]]:
    """Extract a list of records from a page per the template's ExtractSpec."""
    root = Selector(text=html)

    if spec.item_selector:
        items = root.css(spec.item_selector)
    elif spec.item_xpath:
        items = root.xpath(spec.item_xpath)
    else:
        # No item container => treat the whole page as a single record.
        items = [root]

    records: list[dict[str, Any]] = []
    for node in items:
        rec: dict[str, Any] = {}
        for name, fspec in spec.fields.items():
            rec[name] = _extract_field(node, fspec, base_url)
        # Skip records where every field is empty/None.
        if any(v not in (None, "", [], {}) for v in rec.values()):
            records.append(rec)
    return records
