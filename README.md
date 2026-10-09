# Anti-Detection Web Crawler

A configurable crawler you point at **any website with a YAML template**. It has three
advanced capabilities:

1. **Tiered anti-bot-detection** — escalates from cheap TLS-impersonated HTTP up to
   stealth browsers only when a site's defenses force it.
2. **LLM-assisted extraction** — point it at an unknown page and let Claude figure out
   what to extract, no hand-written selectors needed.
3. **Scheduled daily crawling** — with SQLite history and cross-run de-duplication.

> **Authorized use only.** This tool defaults to *polite* crawling: it honors
> `robots.txt`, per-domain rate limits, and concurrency caps. The anti-detection
> features exist because many legitimate targets gate ordinary automated traffic.
> The `--ignore-robots` override is opt-in per job and is logged. **CAPTCHAs are
> never auto-solved** — a CAPTCHA is the site's explicit "human required" gate, so
> the crawler only *detects* one and then, per your config, skips it, pauses for a
> human to solve it, or stops the run (see **CAPTCHA handling** below). Only crawl
> sites you are permitted to crawl.

---

## Setup

Requires **Python 3.11+** (developed and verified on 3.13, Windows). Commands below
use PowerShell; on macOS/Linux swap `.\.venv\Scripts\python.exe` for `.venv/bin/python`.

### 1. Create a virtual environment and install dependencies

```powershell
cd D:\crawler
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Tier 1 (`curl_cffi`) works immediately after this — you can start crawling static and
TLS-fingerprint-gated sites right away.

### 2. (Optional) Install the stealth browsers for Tiers 2 & 3

Only needed for JS-fingerprint / automation-protocol walls. The `patchright` and
`camoufox` **packages** install with step 1; this step downloads their **browser
binaries**:

```powershell
# Tier 2 — Patchright: prefers installed Google Chrome, falls back to bundled Chromium
.\.venv\Scripts\python.exe -m patchright install chromium

# Tier 3 — Camoufox (hardened Firefox; downloads its own build, ~large)
.\.venv\Scripts\python.exe -m camoufox fetch
```

If a browser tier isn't installed, the crawler still runs — it just can't escalate to
that tier and logs a clear message.

> **Version note:** `requirements.txt` pins `playwright==1.50.0` and `pyee>=13,<14`.
> Camoufox's Firefox build speaks an older protocol than the newest Playwright, so the
> newest driver breaks Tier 3; these pins let Patchright and Camoufox coexist. All
> three tiers were verified live against `bot.sannysoft.com`.

### 3. Configure `.env`

```powershell
copy .env.example .env
```

Everything in `.env` is optional (sensible defaults apply). Fill in what you need:

- **Proxies** (strongly recommended for protected sites) — set `PROXY_POOL` to a
  comma-separated list of `http://user:pass@host:port` entries. **Use residential /
  rotating proxies**; datacenter/free proxies have poor IP reputation and make
  Cloudflare-class challenges *harder*, not easier.
- **LLM extraction** — set `ANTHROPIC_API_KEY` to enable `extract.mode: llm`.
- **Storage / logging** — override `CRAWLER_DATA_DIR`, `LOG_LEVEL`, etc. if desired.

### 4. Verify the install

```powershell
# Unit tests (no network needed)
.\.venv\Scripts\python.exe -m pytest -q

# First real crawl of the bundled demo template -> exports a CSV
.\.venv\Scripts\python.exe -m crawler crawl crawler\templates\books.yaml --export csv
```

You should see records land in `data\crawler.db` and a CSV under `data\exports\`.
Re-running the same crawl reports `0 new records` — cross-run de-duplication working.

### 5. Point it at your own site

Copy `crawler\templates\books.yaml`, edit `start_urls` + `extract.fields`, then:

```powershell
.\.venv\Scripts\python.exe -m crawler validate crawler\templates\your-site.yaml
.\.venv\Scripts\python.exe -m crawler crawl    crawler\templates\your-site.yaml --export json
```

See **Templates** below for the full field reference, and **Daily crawling on
Windows** for unattended scheduling.

Copy `.env.example` to `.env` to configure storage, proxies, and the LLM key.

---

## Anti-detection: how it works

Bot walls gate on **layers**, and you only have to satisfy the layer the target
actually checks. The `TieredFetcher` escalates cheap → expensive, and a block at one
tier bumps the URL to the next while rotating the proxy:

| Tier | Engine | Defeats |
|------|--------|---------|
| 1 `http` | `curl_cffi` (`impersonate=chrome`) | TLS/JA3-JA4 + HTTP/2 frame-order fingerprinting |
| 2 `patchright` | Patchright (patched Playwright, real Chrome) | JS fingerprint + CDP/automation-protocol leaks |
| 3 `camoufox` | Camoufox (hardened Firefox) | canvas/WebGL/font entropy, hardest JS targets |

Cross-cutting at every tier: coherent browser-realistic headers, per-domain rate
limiting, proxy rotation with sticky sessions + ban eviction, human-like behavior
(browser tiers), session/cookie persistence, and capped-backoff retries. A "block"
is detected from **content** (e.g. Cloudflare "Just a moment…"), not just status
codes — see `crawler/fetch/blockdetect.py`.

Set the starting tier per template with `fetch.min_tier` (`auto` starts at `http`).

---

## Templates (the "given format and template")

A template is a YAML file describing one target. Example (`crawler/templates/books.yaml`):

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

Field selectors support `::text`, `::attr(name)`, an optional `regex` post-filter,
and `many: true` to collect lists. XPath is available via `xpath:` instead of `css:`.

### LLM extraction mode

Set `extract.mode: llm` and describe what you want — no selectors required:

```yaml
extract:
  mode: llm
  description: "each product's name, price, and rating"
  fields: { name: {css: "x"}, price: {css: "x"}, rating: {css: "x"} }  # optional: pins output keys
```

Needs `ANTHROPIC_API_KEY` in `.env`. Claude receives cleaned page text and returns a
JSON array of records validated against your field names.

---

## Usage

```powershell
# Validate a template parses
.\.venv\Scripts\python.exe -m crawler validate crawler\templates\books.yaml

# Crawl now, export results to CSV
.\.venv\Scripts\python.exe -m crawler crawl crawler\templates\books.yaml --export csv

# List past runs (history + dedupe counts)
.\.venv\Scripts\python.exe -m crawler runs

# Export a specific run to JSON / JSONL / CSV
.\.venv\Scripts\python.exe -m crawler export 1 --format json

# Run the scheduler (blocking) for one or more templates
.\.venv\Scripts\python.exe -m crawler schedule crawler\templates\books.yaml
```

Data lands in SQLite (`data/crawler.db`). Every crawl is a **run**; records carry a
site-scoped content hash so **daily re-runs only store genuinely new/changed items**.

---

## Site-specific scraper: BCS question bank (bcsconfidence.online)

`crawler/sites/bcs_question_bank.py` is a bespoke scraper (not a YAML template) for
`bcsconfidence.online/bcs/question-bank`, whose pages ship their full data as an
Inertia.js JSON payload — one HTTP fetch per exam is enough, no browser tier needed.

```powershell
.\.venv\Scripts\python.exe -m crawler bcs-scrape --ignore-robots --export json
```

- Discovers every exam from the question-bank index, then scrapes each one.
- **Preliminary exams** (`/preli/<slug>`): one record per MCQ question — question
  text, all options, the correct answer, subject, and exam metadata (name, type,
  category, date, duration, marking scheme, total marks).
- **Written exams** (`/written/<slug>`): questions are scanned PDF papers, not
  structured text — records carry exam metadata + the per-subject PDF URL only.
- `--only preli|written`, `--limit N` (cap exams, for testing), `--rate-limit N`
  (req/sec, default 1.0).
- **This domain's `robots.txt` is `Disallow: /`** — the scraper respects that by
  default and refuses to run without `--ignore-robots`. Only pass that flag for a
  site you're authorized to crawl; it's logged on the run either way.

---

## Web dashboard

A local browser UI over the same engine — no separate config needed:

```powershell
.\.venv\Scripts\python.exe -m crawler dashboard          # http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m crawler dashboard --port 9000
```

From the dashboard you can: browse/edit/validate/save/delete **templates**; launch a
**crawl** (choosing CAPTCHA mode and the robots override); watch a **live log stream**
of the active crawl (color-coded by level, auto-scrolling); watch **runs** update live
(auto-refreshing every 3s); click a run to **browse its records**; and **export** any
run to JSON / CSV / JSONL. It's a thin layer that calls the same `crawl` logic, so
CLI and dashboard share one database and template folder.

> **Local only.** The dashboard has no authentication and can trigger crawls, so it
> binds to `127.0.0.1` by default — don't expose it on a public interface without
> putting auth in front of it.

---

## CAPTCHA handling

The crawler **detects** CAPTCHA / human-verification widgets (reCAPTCHA, hCaptcha,
Cloudflare Turnstile, Arkose/FunCaptcha, and generic image/text CAPTCHAs) but **never
auto-solves them** — no solving-farm APIs, no ML breakers. A CAPTCHA is the site's
explicit "human required" gate; you choose how to respond via the template's
`captcha.on_detect` (or the `--captcha` flag on `crawl`):

| Mode | Behavior |
|------|----------|
| `skip` *(default)* | Log the CAPTCHA, mark the URL blocked, and move on. Polite. |
| `manual` | Open a **headed** browser and pause so *you* solve it by hand; the crawl then continues with the resulting session. For sites you're authorized to automate. |
| `stop` | Abort the whole run on the first CAPTCHA. Strict. |

```yaml
# in a template
captcha:
  on_detect: manual
  manual_timeout_sec: 180     # how long to wait for the human in 'manual' mode
```

```powershell
# or override per run
.\.venv\Scripts\python.exe -m crawler crawl crawler\templates\site.yaml --captcha skip
```

Notes:
- When a CAPTCHA appears at the HTTP tier it escalates to a browser tier, since
  `manual` needs a real window. `manual` therefore forces the browser tiers to run
  **headed** and requires a desktop session with a human present.
- Detection and the `skip` / `stop` paths were verified live against Google's
  reCAPTCHA demo; `manual` opens a visible browser for you to complete the challenge.

---

## Daily crawling on Windows

`crawler schedule` is a long-lived blocking process. To run it unattended, register
it with **Task Scheduler**:

1. Create a Basic Task → Trigger: *When the computer starts* (or *daily*).
2. Action: *Start a program*
   - Program: `D:\crawler\.venv\Scripts\python.exe`
   - Arguments: `-m crawler schedule crawler\templates\books.yaml`
   - Start in: `D:\crawler`
3. Check "Run whether user is logged on or not."

Alternatively, if you only want one crawl per day, skip the scheduler and point Task
Scheduler directly at the `crawler crawl ...` command on a daily trigger.

For running the scheduler or dashboard as a service on Windows, Linux (systemd), or
Docker — plus production notes (WSGI server, auth, backups) — see
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

---

## Proxies

Datacenter IPs are the single biggest cause of blocks. Add residential/rotating
proxies in `.env`:

```
PROXY_POOL=http://user:pass@host1:port,http://user:pass@host2:port
```

The pool rotates with sticky per-session IPs and benches proxies that repeatedly
fail. With no proxies set, the crawler uses a direct connection (higher block rate
on hardened sites — expected).

---

## Project layout

```
crawler/
  cli.py            config.py        logging_conf.py
  fetch/    base, http_curl (T1), browser_patchright (T2), browser_camoufox (T3),
            escalation, proxies, behavior, session, headers, blockdetect
  extract/  selectors, llm, (schema via pydantic in config.py)
  crawl/    engine (frontier/dedupe/pagination), robots (politeness)
  store/    db (SQLite + dedupe), export (json/csv/jsonl)
  schedule/ daily (APScheduler), runner (run-one-template)
  templates/ books.yaml
tests/      extract, blockdetect, store, template, escalation
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

18 tests cover extraction, block detection, dedupe, template validation, and tier
escalation — all against local fixtures (no network required).
