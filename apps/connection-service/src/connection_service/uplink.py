"""Cloud-facing side: batch PENDING rows to the backend; mark ACKED only on confirmed receipt."""

from __future__ import annotations

import asyncio
import base64
import logging

import aiohttp
from edgelab_common import Backoff

from connection_service.buffer import Buffer, Row
from connection_service.metrics import UPLOAD_BATCHES, UPSTREAM_CONNECTED

log = logging.getLogger("uplink")


async def writer(queue: asyncio.Queue[Row], buffer: Buffer, max_batch: int = 200) -> None:
    """The only path into SQLite: drain whatever is queued and commit it in one transaction."""
    while True:
        rows = [await queue.get()]
        while len(rows) < max_batch and not queue.empty():
            rows.append(queue.get_nowait())
        try:
            await buffer.insert(rows)
        finally:
            for _ in rows:
                queue.task_done()


async def uplink(
    session: aiohttp.ClientSession, url: str, buffer: Buffer, max_rows: int, max_wait_s: float
) -> None:
    backoff = Backoff(base=0.1, cap=10.0)
    timeout = aiohttp.ClientTimeout(total=5)
    while True:
        rows = await buffer.pending(max_rows)
        if len(rows) < max_rows:  # send at N rows or after T seconds, whichever comes first
            await asyncio.sleep(max_wait_s)
            rows = await buffer.pending(max_rows)
        if not rows:
            continue

        body = {
            "records": [
                {
                    "device_id": r.device_id,
                    "boot_id": r.boot_id,
                    "stream": r.stream,
                    "seq": r.seq,
                    "timestamp_ns": r.timestamp_ns,
                    "payload": base64.b64encode(r.payload).decode(),
                }
                for r in rows
            ]
        }
        try:
            async with session.post(url, json=body, timeout=timeout) as resp:
                resp.raise_for_status()
                result = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as e:
            UPSTREAM_CONNECTED.set(0)
            UPLOAD_BATCHES.labels("error").inc()
            delay = await backoff.sleep()
            log.warning("upload of %d rows failed (%r); retried after %.2fs", len(rows), e, delay)
            continue

        # Only now is it safe to ACK. If we crash before this line the batch is resent,
        # and the backend's idempotency key absorbs the duplicates.
        await buffer.mark_acked([r.id for r in rows])
        UPSTREAM_CONNECTED.set(1)
        UPLOAD_BATCHES.labels("ok").inc()
        if backoff.attempt:
            log.info("upstream recovered")
        backoff.reset()
        log.debug("uploaded %d rows: %s", len(rows), result)
