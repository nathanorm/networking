"""Sequence tracking for lossy streams (UDP)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

_MOD = 1 << 32
_HALF = 1 << 31


class SeqResult(StrEnum):
    FIRST = "first"  # first frame seen for this boot
    OK = "ok"  # exactly last + 1
    GAP = "gap"  # jumped ahead: `missed` frames lost (or still in flight)
    LATE = "late"  # at or behind last: reordered or duplicated
    REBOOT = "reboot"  # new boot_id: sender restarted, seq reset


@dataclass
class SeqTracker:
    """Tracks one sender. Uses serial-number arithmetic (RFC 1982) so u32 wraparound is fine."""

    boot_id: int | None = None
    last: int | None = None

    def observe(self, boot_id: int, seq: int) -> tuple[SeqResult, int]:
        """Returns (result, missed_count)."""
        if boot_id != self.boot_id:
            first = self.boot_id is None
            self.boot_id, self.last = boot_id, seq
            return (SeqResult.FIRST if first else SeqResult.REBOOT), 0

        assert self.last is not None
        diff = (seq - self.last) % _MOD
        if diff == 1:
            self.last = seq
            return SeqResult.OK, 0
        if 0 < diff < _HALF:
            self.last = seq
            return SeqResult.GAP, diff - 1
        return SeqResult.LATE, 0
