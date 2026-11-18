import asyncio
import random
import struct

import pytest
from edgelab_protocol import (
    HEADER_SIZE,
    MAGIC,
    CrcError,
    Frame,
    FrameError,
    MsgType,
    decode,
    encode,
    pack_telemetry,
    read_frame,
)


def make(seq: int = 7) -> Frame:
    return Frame(MsgType.TELEMETRY, 1001, 0xDEADBEEF, seq, 123, pack_telemetry([1.0] * 8))


def test_header_is_28_bytes_and_telemetry_frame_is_64():
    assert HEADER_SIZE == 28
    assert len(encode(make())) == 64


def test_round_trip():
    f = make()
    assert decode(encode(f)) == f


def test_crc_mismatch():
    data = bytearray(encode(make()))
    data[HEADER_SIZE] ^= 1
    with pytest.raises(CrcError):
        decode(bytes(data))


def test_bad_magic():
    data = bytearray(encode(make()))
    data[0] = 0
    with pytest.raises(FrameError, match="magic"):
        decode(bytes(data))


def feed(chunks: list[bytes]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for c in chunks:
        reader.feed_data(c)
    reader.feed_eof()
    return reader


async def read_all(reader: asyncio.StreamReader) -> list[Frame]:
    out = []
    while True:
        try:
            out.append(await read_frame(reader))
        except asyncio.IncompleteReadError:
            return out


async def test_reassembles_one_byte_at_a_time_and_coalesced():
    frames = [make(i) for i in range(20)]
    stream = b"".join(encode(f) for f in frames)

    assert await read_all(feed([bytes([b]) for b in stream])) == frames  # fragmented
    assert await read_all(feed([stream])) == frames  # coalesced

    rng = random.Random(0)
    cuts = sorted(rng.sample(range(1, len(stream)), 30))
    chunks = [stream[a:b] for a, b in zip([0, *cuts], [*cuts, len(stream)], strict=True)]
    assert await read_all(feed(chunks)) == frames  # random splits


async def test_oversized_length_rejected_before_reading_payload():
    head = struct.pack("!HBBIIIQI", MAGIC, 1, MsgType.TELEMETRY, 1, 1, 1, 1, 0xFFFFFFFF)
    with pytest.raises(FrameError, match="payload_len"):
        await read_frame(feed([head]))
