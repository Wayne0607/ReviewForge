"""Tests for loop detector."""

from reviewforge.core.loop_detector import LoopDetector


def test_no_loop_on_different_signatures():
    ld = LoopDetector()
    assert ld.check("reviewer_a:abc") is None
    assert ld.check("reviewer_b:def") is None
    assert ld.check("reviewer_c:ghi") is None


def test_rescue_on_three_identical():
    ld = LoopDetector()
    assert ld.check("scout:aaa") is None
    assert ld.check("scout:aaa") is None
    assert ld.check("scout:aaa") == "rescue"


def test_stall_after_rescue():
    ld = LoopDetector()
    # First rescue
    assert ld.check("scout:aaa") is None
    assert ld.check("scout:aaa") is None
    assert ld.check("scout:aaa") == "rescue"
    # Stall
    assert ld.check("scout:aaa") is None
    assert ld.check("scout:aaa") is None
    assert ld.check("scout:aaa") == "stall"
    assert ld.is_stalled


def test_signature_format():
    sig = LoopDetector.make_signature("security_reviewer", ["a.py", "b.py"])
    assert "security_reviewer" in sig
    assert ":" in sig
