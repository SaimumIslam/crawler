"""Site-specific scrapers that don't fit the generic YAML template model.

The generic `SiteTemplate` (selectors/llm modes over one page's HTML) assumes
a flat list of records per page. Some targets ship deeply nested JSON (e.g.
an SPA's hydration payload) that needs bespoke flattening logic instead - such
scrapers live here and reuse the same fetch/robots/store building blocks.
"""
