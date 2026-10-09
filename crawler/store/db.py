"""SQLite storage: runs, pages, records, and cross-run dedupe by content hash.

Design goals:
- Track every crawl *run* so exports and history are per-run.
- Store each fetched *page* (status, tier used, whether it was blocked).
- Store extracted *records* with a stable content hash so that daily re-runs
  only insert genuinely new/changed items (`record is new` => hash unseen).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    status        TEXT NOT NULL DEFAULT 'running',
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    records_new   INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    url         TEXT NOT NULL,
    status_code INTEGER,
    tier        TEXT,
    blocked     INTEGER NOT NULL DEFAULT 0,
    fetched_at  REAL NOT NULL,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    site          TEXT NOT NULL,
    url           TEXT,
    content_hash  TEXT NOT NULL,
    data          TEXT NOT NULL,          -- JSON blob of the record
    created_at    REAL NOT NULL
);

-- Dedupe key: one row per (site, content_hash) ever seen. Cheap membership test.
CREATE TABLE IF NOT EXISTS seen_hashes (
    site         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    first_run_id INTEGER NOT NULL,
    first_seen   REAL NOT NULL,
    PRIMARY KEY (site, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_records_run  ON records(run_id);
CREATE INDEX IF NOT EXISTS idx_pages_run    ON pages(run_id);
"""


def content_hash(site: str, data: dict[str, Any]) -> str:
    """Stable hash of a record's data (order-independent) scoped to a site."""
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{site}\x00{payload}".encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        ensure_dirs()
        self.path = Path(db_path) if db_path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Concurrent crawl workers share one Store; a single sqlite connection is not
        # safe for simultaneous use, so allow cross-thread use and serialize every
        # access with a lock. DB ops are fast relative to network fetches.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        # Lightweight migration: older databases predate the `trigger` column.
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "trigger" not in cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual'")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ----- runs ------------------------------------------------------------- #

    def start_run(
        self, site: str, notes: str | None = None, trigger: str = "manual"
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO runs(site, started_at, status, notes, trigger) VALUES(?,?,?,?,?)",
                (site, time.time(), "running", notes, trigger),
            )
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, status: str = "completed", notes: str | None = None
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, "
                "notes=COALESCE(?, notes) WHERE id=?",
                (time.time(), status, notes, run_id),
            )

    def bump_run_counts(
        self, run_id: int, pages: int = 0, records_new: int = 0
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET pages_fetched=pages_fetched+?, "
                "records_new=records_new+? WHERE id=?",
                (pages, records_new, run_id),
            )

    def delete_site_data(self, site: str) -> None:
        """Remove a site's runs, pages, records and dedupe hashes."""
        with self._tx() as c:
            run_ids = [r["id"] for r in c.execute("SELECT id FROM runs WHERE site=?", (site,))]
            for rid in run_ids:
                c.execute("DELETE FROM pages WHERE run_id=?", (rid,))
                c.execute("DELETE FROM records WHERE run_id=?", (rid,))
            c.execute("DELETE FROM runs WHERE site=?", (site,))
            c.execute("DELETE FROM seen_hashes WHERE site=?", (site,))

    def list_runs(self, site: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        q = "SELECT * FROM runs"
        args: tuple = ()
        if site:
            q += " WHERE site=?"
            args = (site,)
        q += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            return list(self._conn.execute(q, (*args, limit)))

    # ----- pages ------------------------------------------------------------ #

    def record_page(
        self,
        run_id: int,
        url: str,
        status_code: int | None,
        tier: str | None,
        blocked: bool,
        error: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO pages(run_id,url,status_code,tier,blocked,fetched_at,error)"
                " VALUES(?,?,?,?,?,?,?)",
                (run_id, url, status_code, tier, int(blocked), time.time(), error),
            )

    # ----- records + dedupe ------------------------------------------------- #

    def is_seen(self, site: str, chash: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_hashes WHERE site=? AND content_hash=?",
                (site, chash),
            ).fetchone()
        return row is not None

    def add_record(
        self, run_id: int, site: str, url: str | None, data: dict[str, Any]
    ) -> bool:
        """Insert a record if unseen for this site. Returns True if newly added.

        Race-safe under concurrency: the INSERT OR IGNORE into seen_hashes is the
        atomic gate (its PRIMARY KEY makes the first writer win). Only the writer
        that actually inserted the hash (rowcount == 1) then writes the record, so
        two workers extracting the same item can never both store it.
        """
        chash = content_hash(site, data)
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO seen_hashes"
                "(site,content_hash,first_run_id,first_seen) VALUES(?,?,?,?)",
                (site, chash, run_id, now),
            )
            if cur.rowcount == 0:
                return False  # already seen
            c.execute(
                "INSERT INTO records(run_id,site,url,content_hash,data,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (run_id, site, url, chash, json.dumps(data, ensure_ascii=False), now),
            )
        return True

    def records_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._conn.execute(
                "SELECT url, data, created_at FROM records WHERE run_id=? ORDER BY id",
                (run_id,),
            ))
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d.setdefault("_url", r["url"])
            out.append(d)
        return out

    def latest_run_id(self, site: str | None = None) -> int | None:
        rows = self.list_runs(site=site, limit=1)
        return int(rows[0]["id"]) if rows else None
