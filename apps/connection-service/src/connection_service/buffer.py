"""Crash-safe store-and-forward queue on SQLite (WAL).

Every DB call runs on ONE dedicated thread that owns the connection, so there is exactly one
writer and the event loop never blocks on disk I/O.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

from connection_service.metrics import BUFFER_EVICTED, BUFFER_PENDING

log = logging.getLogger("buffer")

PENDING, ACKED = 0, 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
    id           INTEGER PRIMARY KEY,
    device_id    INTEGER NOT NULL,
    boot_id      INTEGER NOT NULL,
    stream       TEXT    NOT NULL,          -- 'tcp' | 'udp'
    seq          INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    payload      BLOB    NOT NULL,
    status       INTEGER NOT NULL DEFAULT 0, -- 0=PENDING 1=ACKED
    UNIQUE (device_id, boot_id, stream, seq)
);
CREATE INDEX IF NOT EXISTS frames_status ON frames(status, id);
"""


class Row(NamedTuple):
    device_id: int
    boot_id: int
    stream: str
    seq: int
    timestamp_ns: int
    payload: bytes


class StoredRow(NamedTuple):
    id: int
    device_id: int
    boot_id: int
    stream: str
    seq: int
    timestamp_ns: int
    payload: bytes


class Buffer:
    def __init__(self, path: str, max_rows: int) -> None:
        self.path, self.max_rows = path, max_rows
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        self._db: sqlite3.Connection | None = None

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    @property
    def db(self) -> sqlite3.Connection:
        assert self._db is not None, "buffer not open"
        return self._db

    # -- public async API ---------------------------------------------------------------------

    async def open(self) -> None:
        await self._run(self._open)

    async def insert(self, rows: list[Row]) -> None:
        await self._run(self._insert, rows)

    async def pending(self, limit: int) -> list[StoredRow]:
        return await self._run(self._pending, limit)

    async def mark_acked(self, ids: list[int]) -> None:
        await self._run(self._mark_acked, ids)

    async def close(self) -> None:
        await self._run(self._close)
        self._pool.shutdown()

    # -- runs on the sqlite thread ------------------------------------------------------------

    def _open(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer; crash-safe
        db.execute(
            "PRAGMA synchronous=NORMAL"
        )  # survives process crash; may lose last txns on power loss
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(SCHEMA)
        self._db = db
        pending = self._update_pending_gauge()
        log.info("opened %s (%d pending rows recovered)", self.path, pending)

    def _insert(self, rows: list[Row]) -> None:
        with self.db:  # one transaction per batch = group commit
            self.db.executemany(
                "INSERT OR IGNORE INTO frames (device_id, boot_id, stream, seq, timestamp_ns, payload)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._enforce_cap()
        self._update_pending_gauge()

    def _enforce_cap(self) -> None:
        """Never let an outage fill the disk: drop oldest ACKED rows first, then oldest PENDING."""
        excess = self.db.execute("SELECT count(*) FROM frames").fetchone()[0] - self.max_rows
        if excess <= 0:
            return
        for status in (ACKED, PENDING):
            deleted = self.db.execute(
                "DELETE FROM frames WHERE id IN"
                " (SELECT id FROM frames WHERE status = ? ORDER BY id LIMIT ?)",
                (status, excess),
            ).rowcount
            excess -= deleted
            if status == PENDING and deleted:
                BUFFER_EVICTED.inc(deleted)
                log.error("buffer full: evicted %d PENDING rows (data loss)", deleted)
            if excess <= 0:
                return

    def _pending(self, limit: int) -> list[StoredRow]:
        cur = self.db.execute(
            "SELECT id, device_id, boot_id, stream, seq, timestamp_ns, payload FROM frames"
            " WHERE status = ? ORDER BY id LIMIT ?",
            (PENDING, limit),
        )
        return [StoredRow(*r) for r in cur]

    def _mark_acked(self, ids: list[int]) -> None:
        with self.db:
            self.db.executemany(
                "UPDATE frames SET status = ? WHERE id = ?", [(ACKED, i) for i in ids]
            )
        self._update_pending_gauge()

    def _update_pending_gauge(self) -> int:
        n = self.db.execute("SELECT count(*) FROM frames WHERE status = ?", (PENDING,)).fetchone()[
            0
        ]
        BUFFER_PENDING.set(n)
        return n

    def _close(self) -> None:
        if self._db is not None:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()
            self._db = None
