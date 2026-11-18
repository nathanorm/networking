"""Edge gateway: emulator --(TCP/UDP)--> ingest -> SQLite buffer -> batched uplink --> backend."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp
from aiohttp import web
from edgelab_common import env_float, env_int, env_str, setup_logging, wait_for_shutdown
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from connection_service.buffer import Buffer, Row
from connection_service.ingest import UdpIngest, Watchdog, tcp_ingest
from connection_service.uplink import uplink, writer

log = logging.getLogger("connection-service")


async def start_http(port: int) -> web.AppRunner:
    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok\n")

    async def metrics(_: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    setup_logging()
    loop = asyncio.get_running_loop()

    buffer = Buffer(
        env_str("DB_PATH", "/var/lib/edgelab/buffer.db"), env_int("MAX_BUFFER_ROWS", 100_000)
    )
    await buffer.open()
    queue: asyncio.Queue[Row] = asyncio.Queue(maxsize=env_int("INGEST_QUEUE_MAX", 1000))
    watchdog = Watchdog(
        env_float("WATCHDOG_DEGRADED_MS", 500) / 1000, env_float("WATCHDOG_LOST_MS", 2000) / 1000
    )

    udp_transport, _ = await loop.create_datagram_endpoint(
        lambda: UdpIngest(queue, watchdog), local_addr=("0.0.0.0", env_int("UDP_PORT", 9001))
    )
    http = await start_http(env_int("HTTP_PORT", 9100))

    async with aiohttp.ClientSession() as session:
        ingest = [
            asyncio.create_task(
                tcp_ingest(
                    env_str("EMULATOR_HOST", "emulator"),
                    env_int("EMULATOR_TCP_PORT", 9000),
                    env_float("TCP_READ_TIMEOUT_S", 3),
                    queue,
                    watchdog,
                )
            ),
            asyncio.create_task(watchdog.run()),
        ]
        writer_task = asyncio.create_task(writer(queue, buffer))
        uplink_task = asyncio.create_task(
            uplink(
                session,
                env_str("BACKEND_URL", "http://backend-collector:8080") + "/ingest",
                buffer,
                env_int("BATCH_MAX_ROWS", 50),
                env_float("BATCH_MAX_WAIT_MS", 250) / 1000,
            )
        )
        log.info("running")

        sig = await wait_for_shutdown()
        # Graceful shutdown: stop reading -> flush what we already accepted -> close cleanly.
        log.info("%s: stopping ingest", sig.name)
        udp_transport.close()
        for t in ingest:
            t.cancel()
        await asyncio.gather(*ingest, return_exceptions=True)

        log.info("draining %d queued rows into SQLite", queue.qsize())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(queue.join(), timeout=5)

        for t in (writer_task, uplink_task):
            t.cancel()
        await asyncio.gather(writer_task, uplink_task, return_exceptions=True)

    await buffer.close()
    await http.cleanup()
    log.info("clean exit")


if __name__ == "__main__":
    asyncio.run(main())
