"""Evaluation coordination and refusal of incomplete/mixed quality results."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def support(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "scripts/benchmarks"
    monkeypatch.syspath_prepend(str(root))
    return importlib.import_module("benchmark_support")


def test_request_pacing_discards_timestamp_from_previous_boot(support, tmp_path, monkeypatch):
    state = tmp_path / "request-rate"
    state.write_text("500000")
    monkeypatch.setattr(support.time, "monotonic", lambda: 100.0)
    waits = []
    monkeypatch.setattr(support.time, "sleep", waits.append)
    support.wait_for_request_slot(state, 60)
    assert waits == []
    assert state.read_text() == "100.0"


def test_request_pacing_preserves_interval_and_locked_state(support, tmp_path, monkeypatch):
    state = tmp_path / "request-rate"
    state.write_text("100")
    clocks = iter([130.0, 160.0])
    monkeypatch.setattr(support.time, "monotonic", lambda: next(clocks))
    waits = []
    monkeypatch.setattr(support.time, "sleep", waits.append)
    support.wait_for_request_slot(state, 60)
    assert waits == [30.0]
    assert state.read_text() == "160.0"


@pytest.mark.asyncio
async def test_async_request_pacing_cancels_without_claiming_slot_or_holding_lock(support, tmp_path):
    state = tmp_path / "request-rate"
    previous = str(support.time.monotonic())
    state.write_text(previous)
    task = asyncio.create_task(support.wait_for_request_slot_async(state, 30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert state.read_text() == previous
    with support.exclusive_file_lock(state.with_suffix(".lock"), blocking=False):
        pass


@pytest.mark.asyncio
async def test_async_request_pacing_rechecks_shared_slot_after_wait(support, tmp_path, monkeypatch):
    state = tmp_path / "request-rate"
    state.write_text("100")
    clocks, waits = iter([130.0, 160.0]), []
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: next(clocks)))

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(support.asyncio, "sleep", sleep)
    await support.wait_for_request_slot_async(state, 60)
    assert waits == [30.0] and state.read_text() == "160.0"


class _PacingClock:
    """Advance only explicitly released sleepers, without changing asyncio's clock."""

    def __init__(self):
        self.now = 100.0
        self.sleepers = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((asyncio.current_task().get_name(), delay, future))
        await future

    def wake(self, name, at):
        self.now = at
        sleeper = next(item for item in self.sleepers if item[0] == name and not item[2].done())
        sleeper[2].set_result(None)


@pytest.mark.asyncio
async def test_async_pacing_fifo_prevents_later_requests_overtaking_older_waiters(support, tmp_path, monkeypatch):
    state = tmp_path / "request-rate"
    state.write_text("100")
    clock, admitted = _PacingClock(), []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(support.asyncio, "sleep", clock.sleep)

    async def request(name, path):
        await support.wait_for_request_slot_async(path, 30)
        admitted.append((name, clock.now))

    tasks = []
    try:
        for name in ("first", "second", "third"):
            # Equivalent path spelling must share the same local queue.
            path = state if name != "second" else state.parent / "." / state.name
            tasks.append(asyncio.create_task(request(name, path), name=name))
            await real_sleep(0)
        assert [name for name, _, future in clock.sleepers if not future.done()] == ["first"]
        clock.wake("first", 130)
        await real_sleep(0)
        await real_sleep(0)
        tasks.append(asyncio.create_task(request("newcomer", state), name="newcomer"))
        await real_sleep(0)
        for name, at in (("second", 160), ("third", 190), ("newcomer", 220)):
            clock.wake(name, at)
            await real_sleep(0)
            await real_sleep(0)
        await asyncio.gather(*tasks)
        assert admitted == [("first", 130), ("second", 160), ("third", 190), ("newcomer", 220)]
        assert all(delay == 30 for _, delay, _ in clock.sleepers)
        assert state.read_text() == "220"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", ["head", "middle"])
async def test_async_pacing_cancelled_head_or_queued_waiter_does_not_stall_next(
    support, tmp_path, monkeypatch, cancelled
):
    state = tmp_path / "request-rate"
    state.write_text("100")
    clock, admitted = _PacingClock(), []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(support.asyncio, "sleep", clock.sleep)

    async def request(name):
        await support.wait_for_request_slot_async(state, 30)
        admitted.append((name, clock.now))

    tasks = {}
    try:
        for name in ("head", "middle", "tail"):
            tasks[name] = asyncio.create_task(request(name), name=name)
            await real_sleep(0)
        assert [name for name, _, future in clock.sleepers if not future.done()] == ["head"]
        tasks[cancelled].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[cancelled]
        await real_sleep(0)
        assert state.read_text() == "100"
        # The local FIFO lock may stay held, but no process lock is held while sleeping.
        with support.exclusive_file_lock(state.with_suffix(".lock"), blocking=False):
            pass
        survivors = [name for name in tasks if name != cancelled]
        for index, name in enumerate(survivors, 1):
            clock.wake(name, 100 + index * 30)
            await real_sleep(0)
            await real_sleep(0)
        await asyncio.gather(*(tasks[name] for name in survivors))
        assert admitted == [(survivors[0], 130), (survivors[1], 160)]
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_async_fifo_rechecks_other_process_timestamp_without_claiming_future_slot(support, tmp_path, monkeypatch):
    state = tmp_path / "request-rate"
    state.write_text("100")
    clock = _PacingClock()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(support, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(support.asyncio, "sleep", clock.sleep)
    task = asyncio.create_task(support.wait_for_request_slot_async(state, 30), name="waiting")
    try:
        await real_sleep(0)
        assert state.read_text() == "100"
        # Simulate a different process admitting at t=120 while this process waits.
        with support.exclusive_file_lock(state.with_suffix(".lock"), blocking=False):
            state.write_text("120")
        clock.wake("waiting", 130)
        await real_sleep(0)
        assert not task.done() and state.read_text() == "120"
        assert [delay for _, delay, _ in clock.sleepers] == [30, 20]
        clock.wake("waiting", 150)
        await task
        assert state.read_text() == "150"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_async_pacing_same_path_can_be_reused_across_event_loops(support, tmp_path):
    state = tmp_path / "request-rate"

    async def pair():
        await asyncio.gather(*(support.wait_for_request_slot_async(state, 0.001) for _ in range(2)))

    asyncio.run(pair())
    asyncio.run(pair())


def test_portable_lock_excludes_other_process_then_releases(support, tmp_path):
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from benchmark_support import exclusive_file_lock
try:
    with exclusive_file_lock(Path(sys.argv[2]), blocking=False):
        pass
except BlockingIOError:
    raise SystemExit(23)
"""
    lock = tmp_path / "rate.lock"
    command = [sys.executable, "-c", script, str(Path(support.__file__).parent), str(lock)]
    with support.exclusive_file_lock(lock):
        assert subprocess.run(command, capture_output=True, timeout=10).returncode == 23
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 0


def test_inner_partial_is_not_a_completed_review_even_if_runner_returned(support):
    assert support.is_complete_result({"status": "completed", "summary": {"status": "completed"}})
    assert support.is_complete_result({"status": "completed", "summary": {"total_findings": 0, "tasks_failed": 0}})
    assert not support.is_complete_result({"status": "completed", "summary": {"status": "partial"}})
    assert not support.is_complete_result({"status": "completed", "summary": {"status": "duplicate_skipped"}})
    assert not support.is_complete_result({"status": "completed"})
    assert not support.is_complete_result({"status": "completed", "summary": {}})


@pytest.mark.parametrize(
    "changed",
    [
        "source_revision",
        "model",
        "thinking",
        "investigator_thinking",
        "workload_sha256",
        "provider",
        "provider_retry",
        "v4_protocol",
    ],
)
def test_resume_refuses_mixed_evaluation_provenance(support, changed):
    expected = {
        key: "original"
        for key in [
            "source_revision",
            "model",
            "thinking",
            "investigator_thinking",
            "workload_sha256",
            "provider",
            "provider_retry",
            "v4_protocol",
        ]
    }
    previous = {**expected, changed: "other"}
    with pytest.raises(RuntimeError, match="provenance changed"):
        support.validate_resume_metadata(previous, expected, has_results=True)


def test_resume_requires_existing_provenance_but_accepts_identical_parameters(support):
    support.validate_resume_metadata({"model": "same"}, {"model": "same"}, has_results=True)
    with pytest.raises(RuntimeError, match="no provenance"):
        support.validate_resume_metadata(None, {"model": "same"}, has_results=True)


@pytest.mark.asyncio
async def test_judge_refuses_partial_or_missing_requested_reviews_before_llm(support, tmp_path, monkeypatch):
    judge = importlib.import_module("martian_judge")

    def forbidden_judge(*args, **kwargs):
        raise AssertionError("Incomplete runs must not invoke the judge")

    monkeypatch.setattr(judge, "Judge", forbidden_judge)
    workload = tmp_path / "workload.json"
    results = tmp_path / "results.json"
    workload.write_text(json.dumps([{"golden_url": "one"}, {"golden_url": "two"}]))
    results.write_text(json.dumps([{"golden_url": "one", "status": "completed", "summary": {"status": "partial"}}]))
    args = argparse.Namespace(workload=str(workload), reviewforge_results=str(results), limit=0)
    with pytest.raises(RuntimeError, match="2 requested reviews are missing or incomplete"):
        await judge.main_async(args)


def _judge_inputs(tmp_path, *, ledger_recall=False):
    workload = tmp_path / "workload.json"
    results = tmp_path / "results.json"
    qodo = tmp_path / "qodo.json"
    workload.write_text(json.dumps([{"golden_url": "one", "golden_comments": [{"comment": "real issue"}]}]))
    results.write_text(
        json.dumps(
            [
                {
                    "golden_url": "one",
                    "status": "completed",
                    "summary": {"status": "completed"},
                    "head_sha": "head",
                    "review_comments": [{"body": "unmatched published issue"}],
                    "ledger": {
                        "head_sha": "head",
                        "items": {
                            "c": {"status": "confirmed", "claim": "real issue"},
                            "o": {"status": "open", "claim": "open issue"},
                            "u": {"status": "unknown", "claim": "unknown issue"},
                            "r": {"status": "refuted", "claim": "refuted real issue"},
                        },
                    },
                }
            ]
        )
    )
    qodo.write_text(json.dumps({"one": {"qodo-v2": [{"text": "real issue"}]}}))
    return argparse.Namespace(
        workload=str(workload),
        reviewforge_results=str(results),
        qodo_candidates=str(qodo),
        output=str(tmp_path / "judged.json"),
        limit=0,
        concurrency=1,
        thinking="disabled",
        llm_min_interval=60,
        ledger_recall=ledger_recall,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_ledger", [False, True])
async def test_ledger_recall_diagnostics_do_not_change_primary_publication_scores(
    support, tmp_path, monkeypatch, with_ledger
):
    judge = importlib.import_module("martian_judge")
    calls = []

    class FakeJudge:
        model = "same-model"
        input_tokens = output_tokens = 0

        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            pass

        async def match_all(self, goldens, candidates):
            calls.append(candidates)
            return {
                "matches": [
                    {"golden_index": 0, "candidate_index": index, "confidence": 0.9}
                    for index, candidate in enumerate(candidates)
                    if "real issue" in candidate
                ]
            }

    monkeypatch.setattr(judge, "Judge", FakeJudge)
    args = _judge_inputs(tmp_path, ledger_recall=with_ledger)
    await judge.main_async(args)
    state = json.loads(Path(args.output).read_text())
    assert state["status"] == "completed"
    assert set(state["metrics"]) == {"reviewforge", "qodo-v2"}
    assert state["metrics"]["reviewforge"] == {
        "reviews": 1,
        "tp": 0,
        "fp": 1,
        "fn": 1,
        "precision": 0,
        "recall": 0,
        "f1": 0,
    }
    assert state["metrics"]["qodo-v2"]["f1"] == 1
    if with_ledger:
        assert calls[2:] == [["real issue", "open issue", "unknown issue"], ["real issue"], ["refuted real issue"]]
        assert state["ledger_metrics"]["ledger_recall"] == 1
        assert state["ledger_metrics"]["confirmed_recall"] == 1
        assert state["ledger_metrics"]["refuted_goldens"] == 1
    else:
        assert len(calls) == 2
        assert "ledger_metrics" not in state


@pytest.mark.asyncio
async def test_ledger_diagnostic_refuses_unpinned_or_malformed_ledger_before_llm(support, tmp_path, monkeypatch):
    judge = importlib.import_module("martian_judge")
    args = _judge_inputs(tmp_path, ledger_recall=True)
    rows = json.loads(Path(args.reviewforge_results).read_text())
    rows[0]["ledger"]["head_sha"] = "different-head"
    Path(args.reviewforge_results).write_text(json.dumps(rows))

    def forbidden_judge(*args, **kwargs):
        raise AssertionError("Invalid ledger must not invoke the judge")

    monkeypatch.setattr(judge, "Judge", forbidden_judge)
    with pytest.raises(RuntimeError, match="ledger.*head"):
        await judge.main_async(args)
    rows[0]["ledger"]["head_sha"] = "head"
    rows[0]["ledger"]["items"]["u"]["status"] = "invented"
    Path(args.reviewforge_results).write_text(json.dumps(rows))
    with pytest.raises(RuntimeError, match="ledger.*status"):
        await judge.main_async(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_pool", ["publication", "ledger"])
async def test_failed_judge_request_cannot_produce_subset_quality_score(support, tmp_path, monkeypatch, failed_pool):
    judge = importlib.import_module("martian_judge")
    failing = True

    class FailingJudge:
        model = "same-model"
        input_tokens = output_tokens = 0

        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            pass

        async def match_all(self, goldens, candidates):
            if failing and (failed_pool == "publication" or "unknown issue" in candidates):
                return {"error": "429 request rejected"}
            return {"matches": []}

    monkeypatch.setattr(judge, "Judge", FailingJudge)
    args = _judge_inputs(tmp_path, ledger_recall=failed_pool == "ledger")
    with pytest.raises(RuntimeError, match="judge.*incomplete"):
        await judge.main_async(args)
    state = json.loads(Path(args.output).read_text())
    assert state["status"] == "partial"
    assert "metrics" not in state
    assert "ledger_metrics" not in state
    pool = "reviewforge" if failed_pool == "publication" else "ledger"
    assert state["completed"]["one"][pool]["errors"]
    failing = False
    await judge.main_async(args)
    state = json.loads(Path(args.output).read_text())
    assert state["status"] == "completed"
    assert state["metrics"]["reviewforge"]["reviews"] == 1
