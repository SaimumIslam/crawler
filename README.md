# Anti-Detection Web Crawler

A configurable crawler you point at **any website with a YAML template** — or manage
from a local web dashboard (**Crawler Studio**). Three core capabilities:

1. **Tiered anti-bot-detection** — escalates from cheap TLS-impersonated HTTP up to
   stealth browsers only when a site's defenses force it.
2. **LLM-assisted extraction** — point it at an unknown page and let Claude figure out
   what to extract, no hand-written selectors needed.
3. **Scheduled daily crawling** — SQLite history and cross-run de-duplication.

![Crawler Studio — configure a crawler](docs/screenshots/configure.png)

> **Authorized use only.** The crawler defaults to *polite* mode: it honors
> `robots.txt`, per-domain rate limits, and concurrency caps. The anti-detection
> features exist because many legitimate targets gate ordinary automated traffic.
> `--ignore-robots` is opt-in per job and is logged. **CAPTCHAs are never
> auto-solved** — the crawler only *detects* them and then skips, pauses for a human,
> or stops the run (see [CAPTCHA handling](#captcha-handling)). Only crawl sites you
> are permitted to crawl, and respect each site's terms of service and local law.

## Contents

- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Web dashboard](#web-dashboard)
- [Templates](#templates)
- [CLI reference](#cli-reference)
- [Anti-detection tiers](#anti-detection-tiers)
- [CAPTCHA handling](#captcha-handling)
- [Proxies](#proxies)
- [Configuration (`.env`)](#configuration-env)
- [Deployment](#deployment)
- [Project layout](#project-layout)
- [Tests](#tests)
- [License](#license)

## Features

- **YAML templates** — CSS/XPath selectors, regex post-filters, pagination, link following.
- **Three fetch tiers** — `curl_cffi` → Patchright (Chrome) → Camoufox (hardened Firefox), auto-escalating on block.
- **Content-based block detection** — spots Cloudflare-style challenge pages, not just status codes.
- **LLM extraction mode** — describe the data in plain words; Claude returns validated JSON records.
- **Proxy rotation** with sticky sessions and ban eviction.
- **Scheduler** (APScheduler) — `30m`, `12h`, `1d`, or cron expressions.
- **SQLite storage** with site-scoped content hashing — daily re-runs store only new/changed items.
- **Export** to JSON / CSV / JSONL.
- **Local dashboard** — edit templates, launch crawls, stream live logs, browse and export runs.

## Installation

**Requirements:** Python 3.11+ (developed on 3.13), `git`. Works on macOS, Linux and Windows.

### 1. Clone and create a virtual environment

macOS / Linux:

```bash
git clone https://github.com/SaimumIslam/crawler.git
cd crawler
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
git clone https://github.com/SaimumIslam/crawler.git
cd crawler
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

Tier 1 (`curl_cffi`) works right after this — static and TLS-fingerprint-gated sites
are crawlable immediately. The commands below assume the venv is activated.

### 2. (Optional) Install stealth browsers for Tiers 2 & 3

Only needed for JS-fingerprint / automation-protocol walls. The Python packages
install in step 1; this downloads the browser binaries:

```bash
# Tier 2 — Patchright: prefers installed Google Chrome, falls back to bundled Chromium
python -m patchright install chromium

# Tier 3 — Camoufox (hardened Firefox; large download)
python -m camoufox fetch
```

If a browser tier isn't installed the crawler still runs; it just can't escalate to
that tier and logs a clear message.

### 3. (Optional) Configure `.env`

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

Everything is optional. Set `ANTHROPIC_API_KEY` for LLM extraction and `PROXY_POOL`
for protected sites. See [Configuration](#configuration-env).

### 4. Verify

```bash
python -m pytest -q                                              # offline unit tests
python -m crawler crawl crawler/templates/books.yaml --export csv
```

You should see records in `data/crawler.db` and a CSV in `data/exports/`. Re-running
the same crawl reports `0 new records` — de-duplication working.

## Quick start

```bash
# 1. Launch the dashboard
python -m crawler dashboard            # http://127.0.0.1:8000

# 2. ...or use the CLI with your own template
cp crawler/templates/books.yaml crawler/templates/my-site.yaml   # edit start_urls + fields
python -m crawler validate crawler/templates/my-site.yaml
python -m crawler crawl    crawler/templates/my-site.yaml --export json
```

## Web dashboard

```bash
python -m crawler dashboard                # http://127.0.0.1:8000
python -m crawler dashboard --port 9000
```

| Configure | Runs | YAML |
|:--:|:--:|:--:|
| ![Configure](docs/screenshots/configure.png) | ![Runs](docs/screenshots/runs.png) | ![YAML](docs/screenshots/yaml.png) |

From the dashboard you can create/edit/validate/save templates, launch a crawl (with
CAPTCHA mode and robots override), watch a live color-coded log stream, browse a run's
records, and export any run to JSON / CSV / JSONL. It calls the same crawl logic as
the CLI, so both share one database and template folder.

> **Local only.** The dashboard has no authentication and can trigger crawls, so it
> binds to `127.0.0.1` by default. Don't expose it publicly without putting auth in
> front of it.

## Templates

A template is a YAML file describing one target (`crawler/templates/books.yaml`):

```yaml
name: books
start_urls:
  - "https://books.toscrape.com/catalogue/page-1.html"
allow_domains: ["books.toscrape.com"]

follow:
  paginate: "li.next a"      # CSS selector for the "next page" link
  link_selector: null        # optional: enqueue links matching this selector
  max_depth: 1
  max_pages: 3

extract:
  mode: selectors            # or: llm
  item_selector: "article.product_pod"   # one record per matched element
  fields:
    title:  { css: "h3 a::attr(title)" }
    price:  { css: "p.price_color::text", regex: "[0-9.]+" }
    detail_url: { css: "h3 a::attr(href)" }   # relative URLs auto-resolved

fetch:
  min_tier: http             # auto | http | patchright | camoufox
  rate_limit_per_sec: 2.0
  respect_robots: true

schedule:
  every: "1d"                # "30m" | "12h" | "1d" | "cron:0 3 * * *"
  at: "03:00"                # optional wall-clock time for daily runs
```

Selectors support `::text`, `::attr(name)`, an optional `regex` post-filter, and
`many: true` for lists. Use `xpath:` instead of `css:` for XPath.

### LLM extraction mode

Set `extract.mode: llm` and describe what you want — no selectors required:

```yaml
extract:
  mode: llm
  description: "each product's name, price, and rating"
  fields: { name: {css: "x"}, price: {css: "x"}, rating: {css: "x"} }  # optional: pins output keys
```

Requires `ANTHROPIC_API_KEY`. Claude receives cleaned page text and returns a JSON
array validated against your field names.

## CLI reference

```bash
python -m crawler validate <template.yaml>                 # check a template parses
python -m crawler crawl <template.yaml> [--export csv|json|jsonl] [--captcha skip|manual|stop] [--ignore-robots]
python -m crawler runs [--site NAME] [--limit N]           # run history + dedupe counts
python -m crawler export <run_id> --format json [--out PATH]
python -m crawler schedule <template.yaml> ...             # blocking scheduler
python -m crawler dashboard [--host 127.0.0.1] [--port 8000]
```

Data lands in SQLite (`data/crawler.db`). Every crawl is a **run**; records carry a
site-scoped content hash so daily re-runs only store genuinely new or changed items.

The repo also ships a few bespoke site scrapers under `crawler/sites/` (exposed as
extra CLI commands such as `bcs-scrape`). They are site-specific examples; check the
target's `robots.txt` and terms before running them.

## Anti-detection tiers

Bot walls gate on **layers**; you only need to satisfy the layer the target checks.
`TieredFetcher` escalates cheap → expensive, and a block bumps the URL to the next
tier while rotating the proxy:

| Tier | Engine | Defeats |
|------|--------|---------|
| 1 `http` | `curl_cffi` (`impersonate=chrome`) | TLS/JA3-JA4 + HTTP/2 frame-order fingerprinting |
| 2 `patchright` | Patchright (patched Playwright, real Chrome) | JS fingerprint + CDP/automation-protocol leaks |
| 3 `camoufox` | Camoufox (hardened Firefox) | canvas/WebGL/font entropy, hardest JS targets |

Cross-cutting at every tier: coherent browser-realistic headers, per-domain rate
limiting, proxy rotation, human-like behavior (browser tiers), session/cookie
persistence, and capped-backoff retries. Blocks are detected from **content** (e.g.
"Just a moment…"), not just status codes — see `crawler/fetch/blockdetect.py`.

Set the starting tier per template with `fetch.min_tier` (`auto` starts at `http`).

## CAPTCHA handling

CAPTCHAs (reCAPTCHA, hCaptcha, Turnstile, Arkose, generic image/text) are
**detected, never auto-solved**. Choose a response via `captcha.on_detect` in the
template or `--captcha` on `crawl`:

| Mode | Behavior |
|------|----------|
| `skip` *(default)* | Log it, mark the URL blocked, move on. |
| `manual` | Open a **headed** browser and pause so *you* solve it; the crawl continues with that session. Needs a desktop session. |
| `stop` | Abort the whole run on the first CAPTCHA. |

```yaml
captcha:
  on_detect: manual
  manual_timeout_sec: 180
```

## Proxies

Datacenter IPs are the biggest cause of blocks. Use **residential / rotating** proxies:

```
PROXY_POOL=http://user:pass@host1:port,http://user:pass@host2:port
```

The pool rotates with sticky per-session IPs and benches repeatedly failing proxies.
With none set, the crawler connects directly.

## Configuration (`.env`)

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Enables `extract.mode: llm` |
| `ANTHROPIC_MODEL` | Model used for LLM extraction |
| `PROXY_POOL` / `PROXY_URL` | Comma-separated proxy list / single proxy |
| `CRAWLER_DATA_DIR` | Data root (default `data`) |
| `CRAWLER_DB_PATH`, `CRAWLER_EXPORT_DIR`, `CRAWLER_SESSION_DIR`, `CRAWLER_TEMPLATES_DIR` | Per-path overrides |
| `LOG_LEVEL` | `INFO` by default |

Never commit `.env` — it's in `.gitignore`.

## Deployment

Running the scheduler or dashboard as a service (systemd, Windows Task Scheduler,
Docker), plus production notes (WSGI server, auth, backups):
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

## Project layout

```
crawler/
  cli.py            config.py        logging_conf.py
  fetch/     base, http_curl (T1), browser_patchright (T2), browser_camoufox (T3),
             escalation, proxies, behavior, session, headers, blockdetect
  extract/   selectors, llm
  crawl/     engine (frontier/dedupe/pagination), robots (politeness)
  store/     db (SQLite + dedupe), export (json/csv/jsonl)
  schedule/  daily (APScheduler), runner
  sites/     bespoke site scrapers
  templates/ books.yaml, demo.yaml
  web/       Flask app + static dashboard
docs/screenshots/
tests/
```

## Tests

```bash
python -m pytest -q
```

Tests cover extraction, block detection, dedupe, template validation, tier escalation,
rate limiting, CAPTCHA handling and concurrency — all against local fixtures, no
network required.

## Disclaimer

This software is provided for lawful, authorized data collection. You are solely
responsible for how you use it, including compliance with target sites' terms,
`robots.txt`, and applicable laws.

## License

[MIT](LICENSE) © 2026 SaimumIslam
