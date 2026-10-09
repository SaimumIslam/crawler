import pytest
from pydantic import ValidationError

from crawler.config import SiteTemplate


def test_valid_template_defaults_allow_domains():
    t = SiteTemplate.model_validate(
        {
            "name": "t",
            "start_urls": ["https://example.com/a"],
            "extract": {"mode": "selectors", "fields": {"x": {"css": "h1::text"}}},
        }
    )
    assert t.allow_domains == ["example.com"]
    assert t.fetch.min_tier == "auto"
    assert t.fetch.respect_robots is True


def test_selectors_mode_requires_fields():
    with pytest.raises(ValidationError):
        SiteTemplate.model_validate(
            {"name": "t", "start_urls": ["https://e.com"],
             "extract": {"mode": "selectors", "fields": {}}}
        )


def test_llm_mode_requires_description_or_fields():
    with pytest.raises(ValidationError):
        SiteTemplate.model_validate(
            {"name": "t", "start_urls": ["https://e.com"],
             "extract": {"mode": "llm"}}
        )
    # description alone is valid
    t = SiteTemplate.model_validate(
        {"name": "t", "start_urls": ["https://e.com"],
         "extract": {"mode": "llm", "description": "products"}}
    )
    assert t.extract.mode == "llm"


def test_empty_start_urls_rejected():
    with pytest.raises(ValidationError):
        SiteTemplate.model_validate(
            {"name": "t", "start_urls": [],
             "extract": {"mode": "selectors", "fields": {"x": {"css": "a::text"}}}}
        )


def test_books_template_parses():
    t = SiteTemplate.from_yaml("crawler/templates/books.yaml")
    assert t.name == "books"
    assert t.follow.paginate == "li.next a"
