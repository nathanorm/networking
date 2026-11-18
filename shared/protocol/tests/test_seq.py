from edgelab_protocol import SeqResult, SeqTracker


def test_in_order_gap_late_and_reboot():
    t = SeqTracker()
    assert t.observe(1, 0) == (SeqResult.FIRST, 0)
    assert t.observe(1, 1) == (SeqResult.OK, 0)
    assert t.observe(1, 5) == (SeqResult.GAP, 3)
    assert t.observe(1, 3) == (SeqResult.LATE, 0)  # reordered
    assert t.observe(1, 5) == (SeqResult.LATE, 0)  # duplicate
    assert t.observe(2, 0) == (SeqResult.REBOOT, 0)


def test_u32_wraparound_is_not_a_gap():
    t = SeqTracker()
    t.observe(1, 0xFFFFFFFF)
    assert t.observe(1, 0) == (SeqResult.OK, 0)
