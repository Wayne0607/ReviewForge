"""Initial collection gaps must not masquerade as live missing-source status."""

from test_discovery_submission import fixture

from reviewforge.engine.discovery_session import extend_sources, source_packet
from reviewforge.engine.hypothesis import Observation


def test_initial_gaps_are_historical_scoped_and_not_silently_certified_closed():
    _, _, block, aliases = fixture(2)
    first, second = block.units
    pending_gap = f"{first.id}: context caller only 0/2 lines delivered"
    done_gap = f"{second.id}: context caller only 0/2 lines delivered"
    unknown = "unrecognized collection warning must remain visible"
    block.unchecked = [pending_gap, pending_gap, done_gap, unknown]
    initial = source_packet(block, aliases, {first.id}, protocol_version=13)
    assert "unchecked_navigation_only" not in initial
    historical = initial["initial_context_gaps_navigation_only"]
    assert historical["scope"] == "initial-packet-only"
    assert historical["live_coverage"] is False
    assert historical["items"] == [pending_gap, unknown]

    observation = Observation(
        id="obs_0",
        tool="read_file",
        query="read_file consumer.py",
        path="consumer.py",
        line_range=(1, 2),
        result_digest="saved-source-digest",
        sha="head",
        status="success",
        excerpt="first consumer line\nsecond consumer line",
        excerpt_line_range=(1, 2),
    )
    extend_sources(block, observation, aliases)
    after = source_packet(block, aliases, {first.id}, protocol_version=13)
    assert after["initial_context_gaps_navigation_only"] == historical
    assert [row[4] for row in after["source"] if row[2] == "consumer.py"] == [
        "first consumer line",
        "second consumer line",
    ]
    # Older protocols retain their precise historical shape and duplicate text.
    old = source_packet(block, aliases, {first.id}, protocol_version=12)
    assert old["unchecked_navigation_only"] == [pending_gap, pending_gap, done_gap, unknown]
    assert "initial_context_gaps_navigation_only" not in old


def test_empty_initial_gap_list_is_not_a_complete_source_claim():
    _, _, block, aliases = fixture(1)
    block.unchecked = []
    packet = source_packet(block, aliases, {block.units[0].id}, protocol_version=13)
    assert packet["initial_context_gaps_navigation_only"]["items"] == []
    assert packet["initial_context_gaps_navigation_only"]["live_coverage"] is False
    assert packet["source_rows_may_be_partial"] is True
