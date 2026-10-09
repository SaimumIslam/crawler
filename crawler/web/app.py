"""Local web dashboard (Flask) over the crawler engine.

A single-page UI + JSON API to: list/view/edit/validate/save templates, launch
crawls (in a background thread), watch runs live, and browse/export results.

Runs on localhost by design — it exposes the ability to trigger crawls and toggle
robots.txt, so it should not be bound to a public interface without adding auth.
"""
from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify, request, send_file, send_from_directory

from ..config import TEMPLATES_DIR, SiteTemplate, ensure_dirs
from ..logging_conf import current_run_for_thread, get_logger
from ..schedule.daily import _trigger_for
from ..schedule.runner import run_template
from ..store.db import Store
from ..store.export import export_run

log = get_logger("web")

# In-memory record of the most recent crawl launched per site (for live status).
_active: dict[str, dict] = {}
_stops: dict[str, threading.Event] = {}
_lock = threading.Lock()

# In-dashboard scheduler: fires templates that define a `schedule` while the
# dashboard is running. `_sched_next` maps site -> (schedule signature, epoch secs).
_SCHED_POLL_SEC = 5
_sched_enabled = True
_sched_next: dict[str, tuple[tuple, float]] = {}
_sched_started = False

# ---- Live log streaming -------------------------------------------------- #
# A crawl runs in its own background thread; we map that thread -> site so the log
# handler can tag each record, then buffer records in a ring for the UI to poll.
_log_seq = itertools.count(1)
_log_buffer: collections.deque = collections.deque(maxlen=3000)
_thread_site: dict[int, str] = {}
_run_site: dict[int, str] = {}  # run_id -> site, so crawl worker threads can be tagged
_log_lock = threading.Lock()
_IGNORE_LOGGERS = ("werkzeug", "urllib3", "httpx", "asyncio", "flask")


class _DashboardLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] in _IGNORE_LOGGERS:
            return
        run_id = current_run_for_thread(record.thread)
        site = _thread_site.get(record.thread) or _run_site.get(run_id) or "server"
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        with _log_lock:
            _log_buffer.append({
                "seq": next(_log_seq), "ts": record.created, "site": site,
                "run_id": run_id, "level": record.levelname,
                "name": record.name, "msg": msg,
            })


def _install_log_handler() -> None:
    root = logging.getLogger()
    if not any(isinstance(h, _DashboardLogHandler) for h in root.handlers):
        root.addHandler(_DashboardLogHandler())
    # The dashboard wants crawl progress, which is emitted at INFO.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


_STATIC = Path(__file__).parent / "static"


def _safe_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_") or "unnamed"


def _friendly_error(e: Exception) -> str:
    """Turn a pydantic/YAML error into a short, human-readable message."""
    try:
        from pydantic import ValidationError

        if isinstance(e, ValidationError):
            parts = []
            for err in e.errors()[:4]:
                loc = " → ".join(str(p) for p in err.get("loc", ()) if p != "__root__")
                parts.append(f"{loc}: {err.get('msg', '')}".strip(" :"))
            return "; ".join(parts) or str(e)
    except Exception:  # noqa: BLE001
        pass
    return str(e)


# ---- Friendly form <-> template conversion ------------------------------- #
# The dashboard's guided form sends a flat JSON "form"; these map it to/from the
# real template dict so users never have to hand-write YAML.

def _form_to_template_dict(form: dict) -> dict:
    mode = form.get("extract_mode") or "selectors"
    base = form.get("base") or {}  # untouched template settings the form doesn't edit
    fields: dict = {}
    for f in form.get("fields") or []:
        spec = {
            k: v for k, v in f.items()
            if k in ("css", "xpath", "regex", "attr", "many", "default") and v not in (None, "", False)
        }
        if f.get("name") and (spec.get("css") or spec.get("xpath")):
            fields[f["name"]] = spec
    extract: dict = {"mode": mode}
    if form.get("item_selector"):
        extract["item_selector"] = form["item_selector"]
    elif (base.get("extract") or {}).get("item_xpath"):
        extract["item_xpath"] = base["extract"]["item_xpath"]
    if (base.get("extract") or {}).get("infer_selectors"):
        extract["infer_selectors"] = True
    if fields:
        extract["fields"] = fields
    if mode == "llm" and form.get("llm_description"):
        extract["description"] = form["llm_description"]

    follow: dict = {
        "max_depth": int(form.get("max_depth") or 1),
        "max_pages": int(form.get("max_pages") or 500),
    }
    if form.get("link_selector"):
        follow["link_selector"] = form["link_selector"]
    if form.get("paginate"):
        follow["paginate"] = form["paginate"]
    if form.get("link_selector") and (base.get("follow") or {}).get("link_xpath"):
        follow["link_xpath"] = base["follow"]["link_xpath"]

    tdict: dict = {
        "name": (form.get("name") or "").strip(),
        "start_urls": [u.strip() for u in (form.get("start_urls") or []) if u.strip()],
        "extract": extract,
        "follow": follow,
        "fetch": {
            "min_tier": form.get("min_tier") or "auto",
            "rate_limit_per_sec": float(form.get("rate_limit_per_sec") or 1.0),
            "concurrency": int(form.get("concurrency") or 4),
            "respect_robots": bool(form.get("respect_robots", True)),
        },
        "captcha": {"on_detect": form.get("captcha") or "skip"},
    }
    for k in ("timeout_sec", "max_retries", "impersonate", "headless", "render_wait_ms"):
        if k in (base.get("fetch") or {}):
            tdict["fetch"][k] = base["fetch"][k]
    if "manual_timeout_sec" in (base.get("captcha") or {}):
        tdict["captcha"]["manual_timeout_sec"] = base["captcha"]["manual_timeout_sec"]
    if form.get("deny_patterns"):
        tdict["deny_patterns"] = [p for p in form["deny_patterns"] if p.strip()]
    if form.get("schedule_every"):
        sched = {"every": form["schedule_every"]}
        if form.get("schedule_at"):
            sched["at"] = form["schedule_at"]
        if form.get("schedule_timezone"):
            sched["timezone"] = form["schedule_timezone"]
        tdict["schedule"] = sched
    return tdict


def _template_to_form(t: SiteTemplate) -> dict:
    return {
        "name": t.name,
        "start_urls": list(t.start_urls),
        "extract_mode": t.extract.mode,
        "item_selector": t.extract.item_selector or "",
        "fields": [
            {"name": k, **v.model_dump(exclude_none=True, exclude_defaults=True), "css": v.css or ""}
            for k, v in t.extract.fields.items()
        ],
        "llm_description": t.extract.description or "",
        "link_selector": t.follow.link_selector or "",
        "paginate": t.follow.paginate or "",
        "max_depth": t.follow.max_depth,
        "max_pages": t.follow.max_pages,
        "min_tier": t.fetch.min_tier,
        "rate_limit_per_sec": t.fetch.rate_limit_per_sec,
        "concurrency": t.fetch.concurrency,
        "respect_robots": t.fetch.respect_robots,
        "captcha": t.captcha.on_detect,
        "schedule_every": t.schedule.every or "",
        "schedule_at": t.schedule.at or "",
        "schedule_timezone": t.schedule.timezone,
        "deny_patterns": list(t.deny_patterns),
    }


def _launch(tmpl: SiteTemplate, trigger: str = "manual", ignore_robots: bool = False) -> bool:
    """Start a crawl in a background thread. False if that site is already running."""
    with _lock:
        if _active.get(tmpl.name, {}).get("status") == "running":
            return False
        stop = threading.Event()
        _stops[tmpl.name] = stop
        _active[tmpl.name] = {
            "status": "running", "run_id": None, "error": None,
            "trigger": trigger, "started_at": time.time(),
        }

    def _set_run_id(run_id: int) -> None:
        _run_site[run_id] = tmpl.name
        with _lock:
            _active[tmpl.name]["run_id"] = run_id

    def _bg() -> None:
        _thread_site[threading.get_ident()] = tmpl.name
        try:
            res = run_template(
                tmpl, ignore_robots=ignore_robots, stop_event=stop,
                trigger=trigger, on_start=_set_run_id,
            )
            with _lock:
                _active[tmpl.name].update(
                    status="stopped" if res.get("stopped") else "completed",
                    run_id=res["run_id"], pages=res["pages"],
                    records_new=res["records_new"],
                )
        except Exception as e:  # noqa: BLE001
            log.exception("dashboard crawl failed")
            with _lock:
                _active[tmpl.name].update(status="failed", error=str(e))
        finally:
            _thread_site.pop(threading.get_ident(), None)

    threading.Thread(target=_bg, daemon=True).start()
    return True


def _schedule_signature(t: SiteTemplate) -> tuple:
    s = t.schedule
    return (s.every, s.at, s.timezone)


def _scheduler_tick() -> None:
    now = time.time()
    seen: set[str] = set()
    for p in sorted(TEMPLATES_DIR.glob("*.yaml")):
        try:
            tmpl = SiteTemplate.from_yaml(p)
            trigger = _trigger_for(tmpl)
        except Exception:  # noqa: BLE001 - a broken template must not stop the loop
            continue
        if trigger is None:
            continue
        seen.add(tmpl.name)
        sig = _schedule_signature(tmpl)
        cur = _sched_next.get(tmpl.name)
        if cur is None or cur[0] != sig:
            nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            if nxt:
                _sched_next[tmpl.name] = (sig, nxt.timestamp())
            continue
        if now >= cur[1]:
            nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            _sched_next[tmpl.name] = (sig, nxt.timestamp() if nxt else now + 86400)
            if _sched_enabled:
                if _launch(tmpl, trigger="schedule"):
                    log.info("Scheduled run started for '%s'", tmpl.name)
    for name in list(_sched_next):
        if name not in seen:
            del _sched_next[name]


def _scheduler_loop() -> None:
    while True:
        try:
            _scheduler_tick()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        time.sleep(_SCHED_POLL_SEC)


def _start_scheduler() -> None:
    global _sched_started
    if _sched_started:
        return
    _sched_started = True
    threading.Thread(target=_scheduler_loop, name="dashboard-scheduler", daemon=True).start()


def create_app() -> Flask:
    ensure_dirs()
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    _install_log_handler()
    _start_scheduler()
    # static_url_path="" so the page's relative `vendor/...` assets resolve.
    app = Flask(__name__, static_folder=str(_STATIC), static_url_path="")
    app.json.sort_keys = False  # keep record columns in the template's field order

    # ---- UI ------------------------------------------------------------- #
    @app.get("/")
    def index():
        return send_from_directory(str(_STATIC), "index.html")

    # ---- Templates ------------------------------------------------------ #
    @app.get("/api/templates")
    def list_templates():
        items = [{"name": p.stem} for p in sorted(TEMPLATES_DIR.glob("*.yaml"))]
        return jsonify(items)

    @app.get("/api/templates/<name>")
    def get_template(name: str):
        p = TEMPLATES_DIR / f"{_safe_name(name)}.yaml"
        if not p.exists():
            return jsonify({"error": "not found"}), 404
        text = p.read_text(encoding="utf-8")
        form = base = None
        try:
            t = SiteTemplate.from_yaml(p)
            form = _template_to_form(t)
            base = t.model_dump(exclude_none=True, exclude_defaults=True)
        except Exception:  # keep working even if an old template can't map to the form
            pass
        return jsonify({"name": p.stem, "yaml": text, "form": form, "base": base})

    @app.post("/api/compose")
    def compose():
        """Turn the friendly form into validated YAML (live preview / validation)."""
        data = request.get_json(force=True) or {}
        try:
            tdict = _form_to_template_dict(data)
            t = SiteTemplate.model_validate(tdict)
        except Exception as e:
            return jsonify({"ok": False, "error": _friendly_error(e)})
        return jsonify({
            "ok": True,
            "yaml": t.to_yaml(),
            "summary": f"{t.name}: {len(t.start_urls)} start URL(s), "
                       f"mode={t.extract.mode}, captcha={t.captcha.on_detect}",
        })

    @app.post("/api/templates")
    def save_template():
        data = request.get_json(force=True) or {}
        # Accept either a structured form or raw YAML (advanced mode).
        if "form" in data:
            try:
                tdict = _form_to_template_dict(data["form"])
                t = SiteTemplate.model_validate(tdict)
            except Exception as e:
                return jsonify({"error": _friendly_error(e)}), 400
            name, text = _safe_name(t.name), t.to_yaml()
        else:
            name = _safe_name((data.get("name") or "").strip())
            text = data.get("yaml") or ""
            try:
                SiteTemplate.model_validate(yaml.safe_load(text))
            except Exception as e:
                return jsonify({"error": _friendly_error(e)}), 400
        if not name or name == "unnamed":
            return jsonify({"error": "Please give the template a name."}), 400
        (TEMPLATES_DIR / f"{name}.yaml").write_text(text, encoding="utf-8")
        return jsonify({"ok": True, "name": name})

    @app.delete("/api/templates/<name>")
    def delete_template(name: str):
        p = TEMPLATES_DIR / f"{_safe_name(name)}.yaml"
        if p.exists():
            p.unlink()
        if request.args.get("purge") == "1":  # also drop run history + records
            with Store() as st:
                st.delete_site_data(p.stem)
        with _lock:
            if _active.get(p.stem, {}).get("status") != "running":
                _active.pop(p.stem, None)
        _sched_next.pop(p.stem, None)
        return jsonify({"ok": True})

    @app.post("/api/validate")
    def validate():
        data = request.get_json(force=True) or {}
        try:
            t = SiteTemplate.model_validate(yaml.safe_load(data.get("yaml") or ""))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
        return jsonify({
            "ok": True,
            "summary": f"{t.name}: {len(t.start_urls)} start URL(s), "
                       f"mode={t.extract.mode}, captcha={t.captcha.on_detect}",
        })

    # ---- Crawl ---------------------------------------------------------- #
    @app.post("/api/crawl")
    def crawl():
        data = request.get_json(force=True) or {}
        name = _safe_name(data.get("name") or "")
        p = TEMPLATES_DIR / f"{name}.yaml"
        if not p.exists():
            return jsonify({"error": "template not found; save it first"}), 404
        tmpl = SiteTemplate.from_yaml(p)
        captcha = data.get("captcha")
        if captcha in ("skip", "manual", "stop"):
            tmpl.captcha.on_detect = captcha  # type: ignore[assignment]
        ignore_robots = bool(data.get("ignore_robots", False))
        if not _launch(tmpl, trigger="manual", ignore_robots=ignore_robots):
            return jsonify({"error": "a crawl for this site is already running"}), 409
        return jsonify({"ok": True, "site": tmpl.name})

    @app.post("/api/stop")
    def stop():
        name = _safe_name((request.get_json(force=True) or {}).get("name") or "")
        with _lock:
            ev = _stops.get(name)
            running = _active.get(name, {}).get("status") == "running"
        if not (ev and running):
            return jsonify({"error": "that crawler is not running"}), 409
        ev.set()
        return jsonify({"ok": True})

    @app.post("/api/test")
    def test_first_page():
        """Fetch the first start page and show what the template would extract."""
        data = request.get_json(force=True) or {}
        try:
            t = SiteTemplate.model_validate(_form_to_template_dict(data))
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": _friendly_error(e)})
        from ..crawl.engine import CrawlEngine
        from ..extract.selectors import extract_records

        url = t.start_urls[0]
        store = Store()
        engine = CrawlEngine(t, store)
        fetcher = None
        try:
            if not engine._should_visit(url):
                return jsonify({"ok": False, "error": "robots.txt does not allow this page."})
            fetcher = engine._make_fetcher()
            res = fetcher.fetch(url)
            if not res.ok:
                why = "blocked by the site" if res.blocked else f"status {res.status_code}"
                return jsonify({"ok": False, "error": f"Couldn't load the page ({why})."})
            base = res.final_url or url
            if t.extract.mode == "llm":
                from ..extract.llm import LLMExtractor

                records = LLMExtractor().extract(res.text, t.extract, base_url=base)
            else:
                records = extract_records(res.text, t.extract, base_url=base)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": _friendly_error(e)})
        finally:
            if fetcher:
                fetcher.close()
            engine.close()
            store.close()
        return jsonify({"ok": True, "url": url, "count": len(records), "records": records[:3]})

    @app.get("/api/scheduler")
    def scheduler_state():
        return jsonify({
            "enabled": _sched_enabled,
            "next": {k: v[1] for k, v in _sched_next.items()},
        })

    @app.post("/api/scheduler")
    def scheduler_set():
        global _sched_enabled
        _sched_enabled = bool((request.get_json(force=True) or {}).get("enabled", True))
        return jsonify({"ok": True, "enabled": _sched_enabled})

    @app.get("/api/status")
    def status():
        with _lock:
            return jsonify(_active)

    @app.get("/api/logs")
    def logs():
        # Filter by a specific run_id (preferred, precise) or fall back to site.
        run_id = request.args.get("run_id", type=int)
        site = request.args.get("site")
        after = int(request.args.get("after", 0))
        with _log_lock:
            if run_id is not None:
                items = [ln for ln in _log_buffer
                         if ln["seq"] > after and ln.get("run_id") == run_id]
            else:
                items = [ln for ln in _log_buffer
                         if ln["seq"] > after and (not site or ln["site"] == site)]
        last = items[-1]["seq"] if items else after
        return jsonify({"lines": items[-500:], "last": last})

    # ---- Runs / results ------------------------------------------------- #
    @app.get("/api/runs")
    def runs():
        site = request.args.get("site")
        limit = int(request.args.get("limit", 50))
        with Store() as s:
            return jsonify([dict(r) for r in s.list_runs(site=site, limit=limit)])

    @app.get("/api/runs/<int:run_id>")
    def run_detail(run_id: int):
        with Store() as s:
            records = s.records_for_run(run_id)
            run = next((dict(r) for r in s.list_runs(limit=1000) if r["id"] == run_id), None)
        return jsonify({"run": run, "records": records})

    @app.get("/api/runs/<int:run_id>/export")
    def export(run_id: int):
        fmt = request.args.get("format", "json")
        if fmt not in ("json", "jsonl", "csv"):
            return jsonify({"error": "format must be json, jsonl, or csv"}), 400
        with Store() as s:
            path = export_run(s, run_id, fmt=fmt)
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 8000) -> None:
    app = create_app()
    log.info("Crawler dashboard running at http://%s:%d  (Ctrl-C to stop)", host, port)
    app.run(host=host, port=port, threaded=True)
