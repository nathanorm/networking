import random

from edgelab_common import Backoff


def test_full_jitter_stays_within_exponential_cap():
    b = Backoff(base=0.1, cap=1.0, rng=random.Random(0))
    for attempt in range(10):
        assert 0 <= b.next_delay() <= min(1.0, 0.1 * 2**attempt)
    b.reset()
    assert b.attempt == 0
