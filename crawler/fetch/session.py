"""Persist browser storage state (cookies + localStorage) per site+profile.

Reusing storage across runs makes a re-crawl look like a returning visitor, which
helps with sites that grant trust after a first successful challenge solve.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..config import SESSION_DIR, ensure_dirs


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]


def storage_state_path(site: str, profile: str = "default") -> Path:
    ensure_dirs()
    return SESSION_DIR / f"{_safe(site)}__{_safe(profile)}.json"


def existing_state(site: str, profile: str = "default") -> str | None:
    p = storage_state_path(site, profile)
    return str(p) if p.exists() else None
