import base64

from backend_collector.store import Store


def rec(seq: int) -> dict:
    payload = base64.b64encode(b"x").decode()
    return {
        "device_id": 1,
        "boot_id": 2,
        "stream": "tcp",
        "seq": seq,
        "timestamp_ns": seq,
        "payload": payload,
    }


def test_replay_is_idempotent_and_stats_show_gaps(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    assert store.ingest([rec(i) for i in range(5)]) == (5, 0)
    assert store.ingest([rec(i) for i in range(3, 8)]) == (3, 2)  # partial replay
    store.ingest([rec(10)])
    [s] = store.stats()
    assert (s["count"], s["min_seq"], s["max_seq"], s["gaps"]) == (9, 0, 10, 2)
