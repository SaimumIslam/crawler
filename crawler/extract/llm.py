"""LLM-assisted extraction using the Claude API (optional feature).

Given cleaned page text/HTML plus the template's natural-language `description`
and/or declared `fields`, Claude returns a JSON array of records. This lets you
point the crawler at an unknown site without hand-writing selectors.

Requires ANTHROPIC_API_KEY. If the key or the `anthropic` package is missing,
`LLMExtractor` raises a clear error at construction time.
"""
from __future__ import annotations

import json
from typing import Any

from selectolax.parser import HTMLParser

from ..config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ExtractSpec
from ..logging_conf import get_logger

log = get_logger("extract.llm")

_MAX_CHARS = 16000  # keep prompts bounded / cheap


def _clean_text(html: str, max_chars: int = _MAX_CHARS) -> str:
    """Strip scripts/styles and collapse to readable text to shrink the prompt."""
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, svg"):
        tag.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = "\n".join(lines)
    return out[:max_chars]


class LLMExtractor:
    def __init__(self, model: str | None = None) -> None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError(
                "LLM extraction requires ANTHROPIC_API_KEY (set it in .env). "
                "Use extract.mode: selectors for key-free crawling."
            )
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic to use LLM extraction") from e
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.model = model or ANTHROPIC_MODEL

    def _build_prompt(self, page_text: str, spec: ExtractSpec) -> str:
        field_hint = ""
        if spec.fields:
            names = ", ".join(spec.fields.keys())
            field_hint = f"\nEach record MUST use exactly these fields: {names}."
        desc = spec.description or "the main repeated data items on the page"
        return (
            f"Extract {desc} from the page content below.{field_hint}\n"
            "Return ONLY a JSON array of objects, no prose, no markdown fences. "
            "If a field is missing for an item, use null. "
            "If there are no matching items, return [].\n\n"
            "=== PAGE CONTENT ===\n"
            f"{page_text}\n"
            "=== END PAGE CONTENT ==="
        )

    def extract(
        self, html: str, spec: ExtractSpec, base_url: str | None = None
    ) -> list[dict[str, Any]]:
        page_text = _clean_text(html)
        prompt = self._build_prompt(page_text, spec)
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            log.error("LLM extraction call failed: %s", e)
            return []

        raw = "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        ).strip()
        return self._parse_json_array(raw)

    @staticmethod
    def _parse_json_array(raw: str) -> list[dict[str, Any]]:
        if not raw:
            return []
        # Tolerate accidental markdown fences.
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lstrip().lower().startswith("json"):
                raw = raw.lstrip()[4:]
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end == -1 or end < start:
            log.warning("LLM did not return a JSON array")
            return []
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as e:
            log.warning("Could not parse LLM JSON: %s", e)
            return []
        return [d for d in data if isinstance(d, dict)]
