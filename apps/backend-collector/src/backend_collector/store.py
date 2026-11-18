"""Idempotent telemetry store. The primary key IS the idempotency key, so replays are harmless."""

from __future__ import annotations

import base64
import os
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    device_id    INTEGER NOT NULL,
    boot_id      INTEGER NOT NULL,
    stream       TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    payload      BLOB    NOT NULL,
    PRIMARY KEY (device_id, boot_id, stream, seq)
) WITHOUT ROWID;
"""


class Store:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def ingest(self, records: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert a batch atomically. Returns (inserted, duplicates)."""
        rows = [
            (
                r["device_id"],
                r["boot_id"],
                r["stream"],
                r["seq"],
                r["timestamp_ns"],
                base64.b64decode(r["payload"]),
            )
            for r in records
        ]
        with self.db:
            inserted = self.db.executemany(
                "INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?, ?, ?)", rows
            ).rowcount
        return inserted, len(rows) - inserted

    def stats(self) -> list[dict[str, Any]]:
        """Per stream: count, seq range and how many seqs are missing inside that range."""
        cur = self.db.execute(
            "SELECT device_id, boot_id, stream, count(*), min(seq), max(seq) FROM records"
            " GROUP BY device_id, boot_id, stream ORDER BY min(timestamp_ns)"
        )
        return [
            {
                "device_id": d,
                "boot_id": f"{b:#010x}",
                "stream": s,
                "count": n,
                "min_seq": lo,
                "max_seq": hi,
                "gaps": hi - lo + 1 - n,
            }
            for d, b, s, n, lo, hi in cur
        ]
