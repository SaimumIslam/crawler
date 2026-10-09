"""Scheduled crawling via APScheduler.

Reads each template's `schedule` block and registers a job:
  - every: "1d" / "12h" / "30m" / "cron:<expr>"  (with optional `at: HH:MM` for daily)
Runs as a long-lived blocking process. On Windows, register this process with
Task Scheduler / a startup shortcut for unattended daily crawling (see README).
"""
from __future__ import annotations

import re
import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import SiteTemplate
from ..logging_conf import get_logger
from .runner import run_template

log = get_logger("schedule")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _trigger_for(tmpl: SiteTemplate):
    sched = tmpl.schedule
    if not sched.every:
        return None
    every = sched.every.strip().lower()

    if every.startswith("cron:"):
        return CronTrigger.from_crontab(every[5:].strip(), timezone=sched.timezone)

    m = re.fullmatch(r"(\d+)\s*([smhd])", every)
    if not m:
        raise ValueError(f"invalid schedule.every: {sched.every!r}")
    n, unit = int(m.group(1)), m.group(2)

    # Daily with a specific wall-clock time -> cron (more predictable than interval).
    if unit == "d" and sched.at:
        hh, mm = sched.at.split(":")
        return CronTrigger(
            day="*/" + str(n) if n > 1 else "*",
            hour=int(hh), minute=int(mm), timezone=sched.timezone,
        )
    return IntervalTrigger(seconds=n * _UNIT_SECONDS[unit], timezone=sched.timezone)


def run_scheduler(templates: list[SiteTemplate], ignore_robots: bool = False) -> None:
    scheduler = BlockingScheduler()
    scheduled = 0
    for tmpl in templates:
        trigger = _trigger_for(tmpl)
        if trigger is None:
            log.info("Template '%s' has no schedule; skipping", tmpl.name)
            continue
        scheduler.add_job(
            run_template,
            trigger=trigger,
            args=[tmpl, ignore_robots],
            id=tmpl.name,
            name=tmpl.name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduled += 1
        log.info("Scheduled '%s' (%s)", tmpl.name, tmpl.schedule.every)

    if not scheduled:
        log.warning("No templates had a schedule; nothing to run.")
        return

    def _stop(*_):
        log.info("Shutting down scheduler...")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    log.info("Scheduler started with %d job(s). Ctrl-C to stop.", scheduled)
    scheduler.start()
