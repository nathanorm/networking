from connection_service.buffer import Buffer, Row
from connection_service.metrics import BUFFER_EVICTED


def rows(n: int, start: int = 0) -> list[Row]:
    return [Row(1, 1, "tcp", i, i, b"x") for i in range(start, start + n)]


async def test_pending_ack_and_survives_reopen(tmp_path):
    path = str(tmp_path / "buf.db")
    buf = Buffer(path, max_rows=100)
    await buf.open()
    await buf.insert(rows(10))
    await buf.insert(rows(10))  # replayed rows are ignored
    pending = await buf.pending(4)
    assert [r.seq for r in pending] == [0, 1, 2, 3]
    await buf.mark_acked([r.id for r in pending])
    await buf.close()

    buf = Buffer(path, max_rows=100)  # "restart after crash"
    await buf.open()
    assert len(await buf.pending(100)) == 6
    await buf.close()


async def test_cap_evicts_acked_before_pending(tmp_path):
    buf = Buffer(str(tmp_path / "buf.db"), max_rows=10)
    await buf.open()
    await buf.insert(rows(8))
    await buf.mark_acked([r.id for r in await buf.pending(5)])  # 5 acked, 3 pending
    before = BUFFER_EVICTED._value.get()

    await buf.insert(rows(5, start=8))  # 13 rows > cap 10: the 3 oldest ACKED go
    assert len(await buf.pending(100)) == 8
    assert BUFFER_EVICTED._value.get() == before

    await buf.insert(rows(5, start=13))  # remaining 2 ACKED go, then 3 oldest PENDING
    assert [r.seq for r in await buf.pending(100)] == list(range(8, 18))
    assert BUFFER_EVICTED._value.get() == before + 3
    await buf.close()
