from pathlib import Path

from crawler.config import ExtractSpec
from crawler.extract.selectors import extract_records

FIX = Path(__file__).parent / "fixtures" / "catalog.html"


def _spec():
    return ExtractSpec.model_validate(
        {
            "mode": "selectors",
            "item_selector": "article.product_pod",
            "fields": {
                "title": {"css": "h3 a::attr(title)"},
                "price": {"css": "p.price_color::text", "regex": r"[0-9.]+"},
                "availability": {"css": "p.instock.availability::text"},
                "detail_url": {"css": "h3 a::attr(href)"},
            },
        }
    )


def test_extract_records_basic():
    html = FIX.read_text(encoding="utf-8")
    recs = extract_records(html, _spec(), base_url="https://ex.com/catalogue/page-1.html")
    assert len(recs) == 2
    assert recs[0]["title"] == "Alpha Book"
    assert recs[0]["price"] == "12.99"  # regex stripped the currency
    assert recs[0]["availability"] == "In stock"


def test_relative_urls_resolved():
    html = FIX.read_text(encoding="utf-8")
    recs = extract_records(html, _spec(), base_url="https://ex.com/catalogue/page-1.html")
    assert recs[0]["detail_url"] == "https://ex.com/item/aaa.html"


def test_empty_records_skipped():
    spec = ExtractSpec.model_validate(
        {"mode": "selectors", "item_selector": "article.nope",
         "fields": {"title": {"css": "h1::text"}}}
    )
    recs = extract_records("<html><body></body></html>", spec)
    assert recs == []
