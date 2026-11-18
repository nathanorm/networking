"""Mock cloud backend. SQLite calls run on the event loop: fine for a mock, NOT for the edge side."""

from __future__ import annotations

import asyncio
import logging
import random

from aiohttp import web
from edgelab_common import env_float, env_int, env_str, setup_logging, wait_for_shutdown
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    disable_created_metrics,
    generate_latest,
)

from backend_collector.store import Store

log = logging.getLogger("backend-collector")
disable_created_metrics()

INSERTED = Counter("backend_rows_inserted", "New rows stored")
DUPLICATES = Counter("backend_rows_duplicate", "Replayed rows ignored by the idempotency key")
REQUESTS = Counter("backend_ingest_requests", "POST /ingest by response code", ["code"])


def make_app(store: Store, fail_rate: float = 0.0) -> web.Application:
    chaos = {"fail_rate": fail_rate}

    async def ingest(request: web.Request) -> web.Response:
        if random.random() < chaos["fail_rate"]:
            REQUESTS.labels("503").inc()
            raise web.HTTPServiceUnavailable(text="chaos: simulated overload\n")
        records = (await request.json())["records"]
        inserted, dupes = store.ingest(records)
        INSERTED.inc(inserted)
        DUPLICATES.inc(dupes)
        REQUESTS.labels("200").inc()
        if dupes:
            log.info("batch of %d: %d new, %d duplicates (replay)", len(records), inserted, dupes)
        return web.json_response({"inserted": inserted, "duplicates": dupes})

    async def set_chaos(request: web.Request) -> web.Response:
        chaos.update(await request.json())
        log.warning("chaos settings: %s", chaos)
        return web.json_response(chaos)

    async def stats(_: web.Request) -> web.Response:
        return web.json_response(store.stats())

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok\n")

    async def metrics(_: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    app = web.Application()
    app.router.add_post("/ingest", ingest)
    app.router.add_post("/chaos", set_chaos)
    app.router.add_get("/stats", stats)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/metrics", metrics)
    return app


async def main() -> None:
    setup_logging()
    store = Store(env_str("DB_PATH", "/var/lib/edgelab/backend.db"))
    runner = web.AppRunner(make_app(store, env_float("FAIL_RATE", 0.0)), access_log=None)
    await runner.setup()
    port = env_int("HTTP_PORT", 8080)
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("listening on :%d", port)

    sig = await wait_for_shutdown()
    log.info("%s: shutting down", sig.name)
    await runner.cleanup()
    store.db.close()


if __name__ == "__main__":
    asyncio.run(main())
