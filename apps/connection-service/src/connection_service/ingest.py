"""Hardware-facing side: TCP and UDP ingest from the emulator, plus the link watchdog."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time

from edgelab_common import Backoff
from edgelab_protocol import (
    CrcError,
    Frame,
    FrameError,
    MsgType,
    SeqResult,
    SeqTracker,
    decode,
    read_frame,
)

from connection_service.buffer import Row
from connection_service.metrics import (
    CRC_ERRORS,
    EMULATOR_CONNECTS,
    FRAMING_ERRORS,
    LINK_STATE,
    RX_FRAMES,
    UDP_DROPPED,
    UDP_LATE,
    UDP_OVERFLOW,
)

log = logging.getLogger("ingest")


def to_row(frame: Frame, stream: str) -> Row:
    return Row(frame.device_id, frame.boot_id, stream, frame.seq, frame.timestamp_ns, frame.payload)


class Watchdog:
    """Link state per transport from time since the last VALID frame (monotonic clock).

    A link with no data is LOST, not "reading 0.0": never confuse silence with a valid value.
    """

    STATES = ("OK", "DEGRADED", "LOST")

    def __init__(self, degraded_s: float, lost_s: float) -> None:
        self.degraded_s, self.lost_s = degraded_s, lost_s
        self.last_rx: dict[str, float] = {}
        self.state: dict[str, int] = {"tcp": 2, "udp": 2}
        for t, s in self.state.items():
            LINK_STATE.labels(t).set(s)

    def feed(self, transport: str) -> None:
        self.last_rx[transport] = time.monotonic()

    async def run(self) -> None:
        while True:
            now = time.monotonic()
            for transport, prev in self.state.items():
                age = now - self.last_rx.get(transport, float("-inf"))
                state = 0 if age < self.degraded_s else 1 if age < self.lost_s else 2
                if state != prev:
                    level = logging.INFO if state == 0 else logging.WARNING
                    log.log(
                        level, "%s link %s -> %s", transport, self.STATES[prev], self.STATES[state]
                    )
                    self.state[transport] = state
                    LINK_STATE.labels(transport).set(state)
            await asyncio.sleep(0.1)


async def tcp_ingest(
    host: str, port: int, read_timeout: float, queue: asyncio.Queue[Row], watchdog: Watchdog
) -> None:
    backoff = Backoff(base=0.2, cap=5.0)
    while True:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
        except (OSError, TimeoutError) as e:
            delay = await backoff.sleep()
            log.warning("connect %s:%d failed (%s); retried after %.2fs", host, port, e, delay)
            continue

        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        backoff.reset()
        EMULATOR_CONNECTS.inc()
        log.info("connected to emulator %s:%d", host, port)
        try:
            while True:
                try:
                    # Heartbeats arrive every 1s, so silence for read_timeout means the link is
                    # dead, even if the kernel never saw a FIN/RST (half-open connection).
                    frame = await asyncio.wait_for(read_frame(reader), read_timeout)
                except CrcError:
                    CRC_ERRORS.labels("tcp").inc()
                    continue
                RX_FRAMES.labels("tcp", frame.msg_type.name).inc()
                watchdog.feed("tcp")
                if frame.msg_type is MsgType.HELLO:
                    log.info("HELLO device=%d boot_id=%#010x", frame.device_id, frame.boot_id)
                elif frame.msg_type is MsgType.TELEMETRY:
                    await queue.put(to_row(frame, "tcp"))  # blocks when full: TCP backpressure
        except TimeoutError:
            log.warning("no data for %.1fs; assuming link dead, reconnecting", read_timeout)
        except (asyncio.IncompleteReadError, ConnectionError) as e:
            log.warning("emulator closed connection: %r", e)
        except FrameError as e:
            FRAMING_ERRORS.labels("tcp").inc()
            log.error("stream desync (%s); reconnecting", e)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await backoff.sleep()


class UdpIngest(asyncio.DatagramProtocol):
    """One datagram = one frame. Never blocks: UDP has no flow control, so overflow = drop."""

    def __init__(self, queue: asyncio.Queue[Row], watchdog: Watchdog) -> None:
        self.queue, self.watchdog = queue, watchdog
        self.trackers: dict[int, SeqTracker] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            frame = decode(data)
        except CrcError:
            CRC_ERRORS.labels("udp").inc()
            return
        except FrameError as e:
            FRAMING_ERRORS.labels("udp").inc()
            log.debug("bad datagram from %s: %s", addr, e)
            return

        RX_FRAMES.labels("udp", frame.msg_type.name).inc()
        self.watchdog.feed("udp")
        if frame.msg_type is not MsgType.TELEMETRY:
            return

        tracker = self.trackers.setdefault(frame.device_id, SeqTracker())
        result, missed = tracker.observe(frame.boot_id, frame.seq)
        if result is SeqResult.GAP:
            UDP_DROPPED.inc(missed)
        elif result is SeqResult.LATE:
            UDP_LATE.inc()
        elif result is SeqResult.REBOOT:
            log.info("device %d rebooted (boot_id=%#010x)", frame.device_id, frame.boot_id)

        try:
            self.queue.put_nowait(to_row(frame, "udp"))
        except asyncio.QueueFull:
            UDP_OVERFLOW.inc()
