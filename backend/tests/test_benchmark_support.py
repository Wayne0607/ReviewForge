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
