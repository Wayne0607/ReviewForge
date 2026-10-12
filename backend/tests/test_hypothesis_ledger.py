from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from reviewforge.core.database import Database
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Observation, Site


def _hypothesis(line: int, *, severity: str = "warning") -> Hypothesis:
    return Hypothesis(
        id="h_12345678",
        identity="unit::wrong-argument::updateDevice",
        unit_id="unit",
        mechanism=Mechanism.WRONG_ARGUMENT,
        claim="wrong client id",
        trigger="resource is created",
        impact="owner is incorrect",
        open_question="which id is required?",
        refutation="callee overwrites the owner",
        sites=[Site("service.py", line, f"create(client_{line})")],
        severity=severity,
        source="generator",
    )


def test_upsert_merges_identity_sites_and_highest_severity() -> None:
    ledger = HypothesisLedger("run", "sha", "digest")
    assert ledger.upsert(_hypothesis(10))[1] is True
    merged, created = ledger.upsert(_hypothesis(20, severity="error"))
    assert created is False
    assert merged.severity == "error"
    assert [site.line for site in merged.sites] == [10, 20]
    assert HypothesisLedger.from_dict(ledger.to_dict()).to_dict() == ledger.to_dict()


def test_concurrent_upsert_does_not_lose_sites() -> None:
    ledger = HypothesisLedger("run", "sha", "digest")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda line: ledger.upsert(_hypothesis(line)), range(1, 41)))
    assert len(ledger.items[_hypothesis(1).identity].sites) == 40


def test_saved_source_range_roundtrips_without_changing_the_requested_range() -> None:
    hypothesis = _hypothesis(10)
    hypothesis.observations = [
        Observation("obs_0", "read_file", "q", "service.py", (1, 100), "sha", "d", "call()\n", "success", (80, 80))
    ]
    restored = Hypothesis.from_dict(hypothesis.to_dict())
    assert restored.observations[0].line_range == (1, 100)
    assert restored.observations[0].excerpt_line_range == (80, 80)
    old = hypothesis.to_dict()
    del old["observations"][0]["excerpt_line_range"]
    assert Hypothesis.from_dict(old).observations[0].excerpt_line_range is None


@pytest.mark.asyncio
async def test_append_only_resume_uses_latest_revision(tmp_path) -> None:
    db = Database(tmp_path / "reviewforge.db")
    await db.connect()
    try:
        await db.create_run("run", "owner/repo", 1, "sha")
        ledger = HypothesisLedger("run", "sha", "digest")
        first, _ = ledger.upsert(_hypothesis(10))
        await db.append_hypothesis("run", first, head_sha="sha", workspace_digest="digest")
        second, _ = ledger.upsert(_hypothesis(20, severity="error"))
        await db.append_hypothesis("run", second, head_sha="sha", workspace_digest="digest")
        restored = await db.load_hypothesis_ledger("run")
        assert restored is not None
        assert restored.to_dict() == ledger.to_dict()
        cursor = await db._db.execute("SELECT COUNT(*) AS count FROM hypotheses WHERE run_id = 'run'")
        assert (await cursor.fetchone())["count"] == 2
    finally:
        await db.close()


def test_v3_checkpoint_keeps_discovery_and_budget_without_upgrading_old_records():
    from reviewforge.engine.hypothesis import HypothesisLedger

    ledger = HypothesisLedger("run", "sha", "workspace", protocol_version=3)
    ledger.discovery_blocks = {"block": {"unit_ids": ["u1"], "status": "assessed"}}
    ledger.request_budget = {"reported_tokens": 123, "pending": {"request_2": {"reserved_tokens": 500}}}
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    assert restored.protocol_version == 3
    assert restored.discovery_blocks == ledger.discovery_blocks
    assert restored.request_budget == ledger.request_budget
    restored.discovery_blocks["block"]["unit_ids"].append("u2")
    assert ledger.discovery_blocks["block"]["unit_ids"] == ["u1"]
    old = HypothesisLedger.from_dict({"run_id": "old", "head_sha": "sha", "workspace_digest": "d"})
    assert old.protocol_version == 1
    assert "discovery_blocks" not in old.to_dict()
