"""Shared 'run one template end-to-end' function used by CLI and scheduler."""
from __future__ import annotations

import threading
from typing import Callable

from ..config import SiteTemplate
from ..logging_conf import clear_current_run, get_logger, set_current_run
from ..store.db import Store

log = get_logger("runner")


def run_template(
    tmpl: SiteTemplate,
    ignore_robots: bool = False,
    stop_event: threading.Event | None = None,
    trigger: str = "manual",
    on_start: Callable[[int], None] | None = None,
) -> dict:
    """Execute a full crawl run for one template; returns {run_id, pages, records_new}.

    `stop_event` lets a caller (the dashboard) end the run early; the run is then
    recorded as 'stopped' with whatever was collected so far kept.
    """
    from ..crawl.engine import CrawlEngine

    store = Store()
    run_id = store.start_run(tmpl.name, trigger=trigger)
    set_current_run(run_id)  # tag this thread's logs with the run for live streaming
    if on_start:
        on_start(run_id)
    engine = CrawlEngine(tmpl, store, ignore_robots=ignore_robots, stop_event=stop_event)
    try:
        stats = engine.run(run_id)
        stopped = bool(stop_event and stop_event.is_set())
        store.finish_run(run_id, status="stopped" if stopped else "completed")
        log.info(
            "Run %d for '%s' %s: %d pages, %d new records",
            run_id, tmpl.name, "stopped" if stopped else "done",
            stats["pages"], stats["records_new"],
        )
        return {"run_id": run_id, "stopped": stopped, **stats}
    except Exception as e:
        log.exception("Run %d for '%s' failed: %s", run_id, tmpl.name, e)
        store.finish_run(run_id, status="failed", notes=str(e))
        raise
    finally:
        clear_current_run()
        engine.close()
        store.close()
