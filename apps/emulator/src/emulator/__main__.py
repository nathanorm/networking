"""Mock test rig: TCP server for reliable telemetry, UDP sender for high-rate lossy telemetry."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import socket
import struct
import time

from edgelab_common import env_float, env_int, env_str, setup_logging, wait_for_shutdown
from edgelab_protocol import HEADER_SIZE, Frame, MsgType, encode, pack_telemetry

log = logging.getLogger("emulator")


class Emulator:
    def __init__(self) -> None:
        self.device_id = env_int("DEVICE_ID", 1001)
        self.boot_id = random.getrandbits(32)
        self.rate_hz = env_float("SAMPLE_RATE_HZ", 50)
        self.udp_target = (
            env_str("UDP_TARGET_HOST", "connection-service"),
            env_int("UDP_PORT", 9001),
        )
        # Fault injection
        self.udp_drop_pct = env_float("FAULT_UDP_DROP_PCT", 0)
        self.crc_corrupt_pct = env_float("FAULT_CRC_CORRUPT_PCT", 0)
        self.tcp_fragment_bytes = env_int("FAULT_TCP_FRAGMENT_BYTES", 0)
        self.tcp_seq = 0
        self.udp_seq = 0

    # -- frames -------------------------------------------------------------------------------

    def frame(self, msg_type: MsgType, seq: int = 0, payload: bytes = b"") -> bytes:
        data = encode(Frame(msg_type, self.device_id, self.boot_id, seq, time.time_ns(), payload))
        if payload and self._chance(self.crc_corrupt_pct):
            corrupted = bytearray(data)
            corrupted[HEADER_SIZE] ^= 0x01  # flip one payload bit after the CRC was computed
            data = bytes(corrupted)
        return data

    def reading(self) -> bytes:
        t = time.monotonic()
        return pack_telemetry(
            [
                math.sin(t), math.cos(t), 9.81,  # accel
                0.1 * math.sin(3 * t), 0.0, 0.0,  # gyro
                25 + math.sin(t / 10),  # temp_c
                12.0 + random.gauss(0, 0.05),  # bus_v
            ]
        )  # fmt: skip

    @staticmethod
    def _chance(pct: float) -> bool:
        return pct > 0 and random.random() * 100 < pct

    # -- TCP ----------------------------------------------------------------------------------

    async def handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        # Small, latency-sensitive frames: don't let Nagle batch them.
        writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log.info("client connected: %s", peer)
        period = 1 / self.rate_hz
        try:
            await self.send(
                writer, self.frame(MsgType.HELLO, payload=struct.pack("!H", int(self.rate_hz)))
            )
            next_tick = next_heartbeat = time.monotonic()
            while True:
                await self.send(writer, self.frame(MsgType.TELEMETRY, self.tcp_seq, self.reading()))
                self.tcp_seq += 1
                if time.monotonic() >= next_heartbeat:
                    await self.send(writer, self.frame(MsgType.HEARTBEAT))
                    next_heartbeat += 1.0
                next_tick += period
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        except (ConnectionError, OSError) as e:
            log.info("client %s gone: %r", peer, e)
        finally:
            writer.close()

    async def send(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        n = self.tcp_fragment_bytes
        if n <= 0:
            writer.write(data)
            await writer.drain()  # blocks when the peer's receive window is full: backpressure
            return
        for i in range(0, len(data), n):  # FAULT: dribble the frame out a few bytes at a time
            writer.write(data[i : i + n])
            await writer.drain()
            await asyncio.sleep(0.005)

    # -- UDP ----------------------------------------------------------------------------------

    async def udp_loop(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0)
        )
        host, port = self.udp_target
        addr, resolve_at = None, 0.0
        period = 1 / self.rate_hz
        next_tick = time.monotonic()
        try:
            while True:
                # Re-resolve periodically: the receiver's container IP changes when it restarts.
                if time.monotonic() >= resolve_at:
                    try:
                        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
                        addr = infos[0][4]
                    except OSError as e:
                        log.warning("cannot resolve %s: %s", host, e)
                    resolve_at = time.monotonic() + 5
                frame = self.frame(MsgType.TELEMETRY, self.udp_seq, self.reading())
                self.udp_seq += 1
                if addr and not self._chance(self.udp_drop_pct):
                    transport.sendto(frame, addr)  # fire and forget: no ack, no retry
                next_tick += period
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        finally:
            transport.close()


async def main() -> None:
    setup_logging()
    emu = Emulator()
    port = env_int("TCP_PORT", 9000)
    server = await asyncio.start_server(emu.handle_tcp, "0.0.0.0", port)
    udp = asyncio.create_task(emu.udp_loop())
    log.info(
        "device=%d boot_id=%#010x rate=%sHz tcp=:%d udp->%s:%d faults(udp_drop=%s%% crc=%s%% frag=%sB)",
        emu.device_id, emu.boot_id, emu.rate_hz, port, *emu.udp_target,
        emu.udp_drop_pct, emu.crc_corrupt_pct, emu.tcp_fragment_bytes,
    )  # fmt: skip

    sig = await wait_for_shutdown()
    log.info("%s received, shutting down", sig.name)
    udp.cancel()
    server.close()
    server.close_clients()
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
