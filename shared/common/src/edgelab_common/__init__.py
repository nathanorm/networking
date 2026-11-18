"""Small runtime helpers shared by every app."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def setup_logging() -> None:
    logging.basicConfig(
        level=env_str("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


class Backoff:
    """Exponential backoff with full jitter: sleep ~ U(0, min(cap, base * 2^attempt)).

    The jitter spreads out a fleet of clients that all lost the same backend at the same moment,
    so they don't reconnect in lockstep (thundering herd).
    """

    def __init__(self, base: float = 0.1, cap: float = 10.0, rng: random.Random | None = None):
        self.base, self.cap, self.attempt = base, cap, 0
        self._rng = rng or random.Random()

    def next_delay(self) -> float:
        delay = self._rng.uniform(0, min(self.cap, self.base * 2**self.attempt))
        self.attempt += 1
        return delay

    async def sleep(self) -> float:
        delay = self.next_delay()
        await asyncio.sleep(delay)
        return delay

    def reset(self) -> None:
        self.attempt = 0


async def wait_for_shutdown() -> signal.Signals:
    """Block until SIGTERM (docker stop) or SIGINT (Ctrl-C). Returns the signal received."""
    loop = asyncio.get_running_loop()
    received: asyncio.Future[signal.Signals] = loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: received.done() or received.set_result(s))
    return await received
