"""Wire format: header (28 B) + payload + CRC32 (4 B), all network byte order."""

from __future__ import annotations

import asyncio
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

MAGIC = 0xCA01
VERSION = 1
MAX_PAYLOAD_SIZE = 4096

#             magic ver type device boot seq  ts  len
HEADER = struct.Struct("!HBBIIIQI")
CRC = struct.Struct("!I")
TELEMETRY = struct.Struct("!8f")  # ax ay az gx gy gz temp_c bus_v

HEADER_SIZE = HEADER.size  # 28
CRC_SIZE = CRC.size  # 4


class MsgType(IntEnum):
    HELLO = 1
    TELEMETRY = 2
    HEARTBEAT = 3
    COMMAND = 4
    COMMAND_ACK = 5


class FrameError(ValueError):
    """Malformed frame. On TCP this means the stream is desynced."""


class CrcError(FrameError):
    """Length was plausible but the checksum didn't match: drop the frame, framing is intact."""


@dataclass(frozen=True, slots=True)
class Frame:
    msg_type: MsgType
    device_id: int
    boot_id: int
    seq: int
    timestamp_ns: int
    payload: bytes = b""


def encode(frame: Frame) -> bytes:
    head = HEADER.pack(
        MAGIC,
        VERSION,
        frame.msg_type,
        frame.device_id,
        frame.boot_id,
        frame.seq,
        frame.timestamp_ns,
        len(frame.payload),
    )
    body = head + frame.payload
    return body + CRC.pack(zlib.crc32(body))


def _payload_len(head: bytes) -> int:
    """Validate a header and return its payload length. Checked BEFORE reading the payload,
    so a corrupt length can't make us allocate gigabytes."""
    magic, version, msg_type, *_, length = HEADER.unpack(head)
    if magic != MAGIC:
        raise FrameError(f"bad magic {magic:#06x}")
    if version != VERSION:
        raise FrameError(f"unsupported version {version}")
    if msg_type not in MsgType._value2member_map_:
        raise FrameError(f"unknown msg_type {msg_type}")
    if length > MAX_PAYLOAD_SIZE:
        raise FrameError(f"payload_len {length} > {MAX_PAYLOAD_SIZE}")
    return length


def decode(buf: bytes) -> Frame:
    """Decode exactly one complete frame (e.g. a UDP datagram)."""
    if len(buf) < HEADER_SIZE + CRC_SIZE:
        raise FrameError(f"short frame ({len(buf)} B)")
    length = _payload_len(buf[:HEADER_SIZE])
    if len(buf) != HEADER_SIZE + length + CRC_SIZE:
        raise FrameError(f"length mismatch: header says {length}, got {len(buf)} B total")
    body, (crc,) = buf[:-CRC_SIZE], CRC.unpack(buf[-CRC_SIZE:])
    if zlib.crc32(body) != crc:
        raise CrcError("crc mismatch")
    _, _, msg_type, device_id, boot_id, seq, ts, _ = HEADER.unpack(buf[:HEADER_SIZE])
    return Frame(MsgType(msg_type), device_id, boot_id, seq, ts, bytes(body[HEADER_SIZE:]))


async def read_frame(reader: asyncio.StreamReader) -> Frame:
    """TCP is a byte stream, not messages: read exactly a header, then exactly what it announces."""
    head = await reader.readexactly(HEADER_SIZE)
    length = _payload_len(head)
    rest = await reader.readexactly(length + CRC_SIZE)
    return decode(head + rest)


def pack_telemetry(values: Sequence[float]) -> bytes:
    return TELEMETRY.pack(*values)


def unpack_telemetry(payload: bytes) -> tuple[float, ...]:
    return TELEMETRY.unpack(payload)
