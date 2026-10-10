"""Evaluation coordination and refusal of incomplete/mixed quality results."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path

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


@pytest.mark.parametrize("changed", ["source_revision", "model", "thinking", "workload_sha256", "provider"])
def test_resume_refuses_mixed_evaluation_provenance(support, changed):
    expected = {key: "original" for key in ["source_revision", "model", "thinking", "workload_sha256", "provider"]}
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
