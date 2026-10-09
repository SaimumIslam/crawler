# Deployment Guide

How to run this crawler in the three ways it's meant to run — a **one-off / manual
crawl**, an **unattended scheduler** (daily crawling), and the **web dashboard** —
on Windows, Linux, or Docker, including how to run each as a long-lived service.

> Read `README.md` first for what the tool does and the template format. This file
> is only about *deploying and operating* it.

---

## 1. Prerequisites

- **Python 3.11+** (developed/verified on 3.13).
- Optional, only for the stealth browser tiers (2 & 3): a machine that can run a
  browser. Headless works on servers; **`manual` CAPTCHA mode needs a real desktop
  session** (a visible window), so don't use `manual` on a headless server.
- For proxies: residential/rotating proxy credentials (see README → Proxies).

---

## 2. Install (all platforms)

```bash
# clone / copy the project, then:
python -m venv .venv
# Windows:
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Linux/macOS:
./.venv/bin/python -m pip install -r requirements.txt
```

Tier 1 (`curl_cffi`) works immediately. For the browser tiers, fetch their binaries
once (see the version note in `requirements.txt`):

```bash
python -m patchright install chromium     # Tier 2
python -m camoufox fetch                  # Tier 3
# Linux servers also need system libs for the browsers, e.g. on Debian/Ubuntu:
#   python -m playwright install-deps
```

Then create `.env` from the template and fill in proxies / `ANTHROPIC_API_KEY` /
storage paths as needed:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Verify the install:

```bash
python -m pytest -q
python -m crawler crawl crawler/templates/books.yaml --export csv
```

---

## 3. Storage & configuration for servers

All state lives under `CRAWLER_DATA_DIR` (default `./data`): the SQLite DB
(`crawler.db`), exports, and browser sessions. For a server deployment, put it on a
persistent, backed-up path and set it explicitly in `.env`:

```
CRAWLER_DATA_DIR=/var/lib/crawler/data
CRAWLER_TEMPLATES_DIR=/etc/crawler/templates
LOG_LEVEL=INFO
```

**Backups:** the SQLite DB is the source of truth. Back it up with the online
backup command (safe while running, thanks to WAL mode):

```bash
sqlite3 /var/lib/crawler/data/crawler.db ".backup '/backups/crawler-$(date +%F).db'"
```

**Secrets:** `.env` holds proxy credentials and the API key — keep it out of version
control (it's already in `.gitignore`) and lock its permissions (`chmod 600 .env`).

---

## 4. Deploy the scheduler (unattended daily crawling)

`crawler schedule <templates...>` is a long-lived process that runs each template on
its `schedule:` block. Deploy it as a service so it survives reboots.

### Windows — Task Scheduler

1. Task Scheduler → **Create Task** (not Basic).
2. **General:** "Run whether user is logged on or not"; check "Run with highest
   privileges" only if needed.
3. **Triggers:** *At startup* (the scheduler process then handles the daily timing
   itself). Optionally add a delay.
4. **Actions:** *Start a program*
   - Program: `D:\crawler\.venv\Scripts\python.exe`
   - Arguments: `-m crawler schedule crawler\templates\books.yaml`
   - Start in: `D:\crawler`
5. **Settings:** "If the task fails, restart every 1 minute, up to 3 times"; and
   "Do not stop the task" so it stays resident.

> If you only want a single crawl per day (no resident process), point Task Scheduler
> at `-m crawler crawl <template>` on a **Daily** trigger instead, and drop the
> `schedule:` block from the template.

### Linux — systemd

`/etc/systemd/system/crawler-scheduler.service`:

```ini
[Unit]
Description=Web crawler scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=crawler
WorkingDirectory=/opt/crawler
EnvironmentFile=/opt/crawler/.env
ExecStart=/opt/crawler/.venv/bin/python -m crawler schedule /etc/crawler/templates/books.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crawler-scheduler
journalctl -u crawler-scheduler -f      # live logs
```

---

## 5. Deploy the web dashboard

```bash
python -m crawler dashboard --host 127.0.0.1 --port 8000
```

### Important: it has no authentication

The dashboard can trigger crawls and toggle robots.txt, so **never bind it directly
to a public interface.** Two safe patterns:

- **Local only (default):** keep `--host 127.0.0.1` and access it over an SSH tunnel:
  `ssh -L 8000:127.0.0.1:8000 user@server`, then browse `http://localhost:8000`.
- **Behind a reverse proxy with auth:** bind to localhost and put nginx/Caddy in
  front with HTTP basic auth or SSO + TLS.

### Use a production WSGI server (not the Flask dev server)

`crawler dashboard` runs Flask's built-in server, which is fine for personal/local
use but not for a shared deployment. For production, serve the app factory with a
real WSGI server (add it to your venv; it's not a default dependency):

```bash
# Linux
pip install gunicorn
gunicorn --workers 1 --threads 8 --bind 127.0.0.1:8000 "crawler.web.app:create_app()"

# Windows
pip install waitress
waitress-serve --listen=127.0.0.1:8000 --call crawler.web.app:create_app
```

> Use **one worker process** (with threads). Crawl state, the live-log buffer, and
> the "is a crawl running" flags are in-memory per process; multiple worker
> *processes* would each see only their own crawls. Threads within one process are
> fine and are what the app is built for.

### systemd unit for the dashboard (behind nginx)

```ini
[Unit]
Description=Crawler dashboard
After=network-online.target

[Service]
User=crawler
WorkingDirectory=/opt/crawler
EnvironmentFile=/opt/crawler/.env
ExecStart=/opt/crawler/.venv/bin/gunicorn --workers 1 --threads 8 \
          --bind 127.0.0.1:8000 "crawler.web.app:create_app()"
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

nginx snippet adding auth + TLS in front:

```nginx
location / {
    auth_basic "Crawler";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8000;
    proxy_read_timeout 300s;   # long crawls stream logs; keep the connection alive
}
```

---

## 6. Docker (optional)

`Dockerfile`:

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
# For the browser tiers, also install their binaries + system deps here:
#   RUN python -m patchright install --with-deps chromium && python -m camoufox fetch
COPY . .
ENV CRAWLER_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
# Dashboard by default; override the command to run the scheduler instead.
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:8000", "crawler.web.app:create_app()"]
```

```bash
docker build -t crawler .
# Dashboard (keep it behind an authenticated proxy; -p binds to localhost here):
docker run -d --env-file .env -v crawler-data:/data -p 127.0.0.1:8000:8000 crawler
# Scheduler instead:
docker run -d --env-file .env -v crawler-data:/data crawler \
  python -m crawler schedule /app/crawler/templates/books.yaml
```

Persist `/data` with a named volume so the DB and exports survive container
restarts. The browser tiers make the image large and need extra system libraries —
if you only crawl static/TLS-gated sites, skip the browser install lines.

---

## 7. Operating notes

- **Concurrency:** set per template via `fetch.concurrency` (default 4). Each worker
  owns its own fetcher, so with the browser tiers, concurrency N can launch up to N
  browsers — size it to the machine. `manual` CAPTCHA mode is forced to 1 worker.
- **Rate limiting is per domain** (`fetch.rate_limit_per_sec`) and independent across
  domains, so concurrency helps most when a crawl spans many hosts.
- **Proxies** are the biggest lever for protected sites; a single process shares one
  proxy pool across all workers (rotation + ban eviction).
- **Logs:** the scheduler/CLI log to stdout (capture via systemd/journald or a file
  redirect). The dashboard also buffers recent per-run logs in memory for its live
  view — that buffer is not persistent; the SQLite `runs`/`pages` tables are.
- **Health check** for the dashboard: `GET /api/status` returns JSON and is cheap.
