"""Central logging configuration."""
from __future__ import annotations

import logging
import os
import threading

_CONFIGURED = False

# ---- Per-thread "current run" context -------------------------------------- #
# A crawl runs in one thread; tagging that thread with its run_id lets the web
# dashboard's log handler attribute each log line to a specific run.
_run_ctx: dict[int, int] = {}
_run_ctx_lock = threading.Lock()


def set_current_run(run_id: int) -> None:
    with _run_ctx_lock:
        _run_ctx[threading.get_ident()] = run_id


def clear_current_run() -> None:
    with _run_ctx_lock:
        _run_ctx.pop(threading.get_ident(), None)


def current_run_for_thread(thread_id: int) -> int | None:
    with _run_ctx_lock:
        return _run_ctx.get(thread_id)


def configure(level: str | None = None) -> None:
    """Configure root logging once. Level from arg or LOG_LEVEL env (default INFO)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "urllib3", "asyncio", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(name)
